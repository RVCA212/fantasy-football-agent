import os
from typing import TypedDict, Dict, Callable
from langchain_aws import ChatBedrockConverse
from langchain_openai import ChatOpenAI
from langgraph.graph import MessagesState
from langchain_core.messages import RemoveMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.store.base import BaseStore
from langchain_core.runnables.config import RunnableConfig
from langchain_core.tools import BaseTool, tool as create_tool

from langgraph.graph import START, END, StateGraph
from langgraph.prebuilt import tools_condition
from langgraph.checkpoint.memory import MemorySaver
from langgraph.store.memory import InMemoryStore
from pydantic_core import ValidationError

from sleeper import SleeperClient
from league import League
import config as cf
from prompts import *
from graph_config import Configuration

# long-term memory

class UserProfile(TypedDict):
    team_name: str
    current_concerns: str
    other_details: str


class SummarizedMessagesState(MessagesState):
    summary: str

try:
    llm = ChatOpenAI(
        model="o3-mini",
        api_key=os.environ.get('OPENAI_API_KEY'),
    )
except ValidationError:
    llm = ChatOpenAI(model="o3-mini", api_key=os.environ.get('OPENAI_API_KEY'), temperature=0)

llm_with_structure = llm.with_structured_output(UserProfile)

sleeper = SleeperClient()

def get_tools(league: League = League(cf.DEFAULT_LEAGUE_ID)) -> list[BaseTool]:
    tools = [
        league.get_league_status,
        league.get_roster_for_team_owner,
        league.get_player_news,
        league.get_player_stats,
        league.get_player_current_owner,
        league.get_best_available_at_position,
        league.get_player_rankings,
    ]
    return [create_tool(t) for t in tools]


def assistant(state: SummarizedMessagesState, config: RunnableConfig, store: BaseStore):

    # Get the user ID from the config
    username = config["configurable"]["username"]
    user_leagues = sleeper.get_leagues_for_user(sleeper.get_user(username)['user_id'])
    league_id = config["configurable"].get("league_id", user_leagues[0]['league_id'])

    league = League(league_id=league_id)

    # Retrieve memory from the store
    namespace = ("memory", username)
    existing_memory = store.get(namespace, "user_memory")

    memory_value = existing_memory.value if existing_memory else 'No memory found'

    messages = [SystemMessage(ASSISTANT_INSTRUCTION.format(username=username, memory=memory_value))] + state["messages"]

    llm_with_tools = llm.bind_tools(get_tools(league))

    return {"messages": [llm_with_tools.invoke(messages)]}


def summarize(state: SummarizedMessagesState, config: RunnableConfig, store: BaseStore):
    summary = state.get("summary", "")

    # Create our summarization prompt
    if summary:
        # A summary already exists
        summary_message = (
            f"This is summary of the conversation to date: {summary}\n\n"
            "Extend the summary by taking into account the new messages above:"
        )

    else:
        summary_message = "Create a summary of the conversation above:"

    # Add prompt to our history
    messages = state["messages"] + [HumanMessage(content=summary_message)]
    response = llm.invoke(messages)

    # Delete all but the 2 most recent messages
    delete_messages = [RemoveMessage(id=m.id) for m in state["messages"][:-2]]
    return {"summary": response.content, "messages": delete_messages}


def write_memory(state: MessagesState, config: RunnableConfig, store: BaseStore):
    """Reflect on the chat history and save a memory to the store."""

    # Get the user ID from the config
    username = config["configurable"]["username"]

    # Retrieve existing memory from the store
    namespace = ("memory", username)
    existing_memory = store.get(namespace, "user_memory")

    # Format the memories for the system prompt
    if existing_memory and existing_memory.value:
        memory_dict = existing_memory.value
        formatted_memory = (
            f"Team Name: {memory_dict.get('team_name', 'Unknown')}\n"
            f"Current Concerns: {memory_dict.get('current_concerns', 'Unknown')}"
            f"Other Details: {memory_dict.get('other_details', 'Unknown')}"
        )
    else:
        formatted_memory = None

    # Format the existing memory in the instruction
    system_msg = CREATE_MEMORY_INSTRUCTION.format(memory=formatted_memory)

    # Invoke the model to produce structured output that matches the schema
    new_memory = llm_with_structure.invoke([SystemMessage(content=system_msg)] + state['messages'])

    # Overwrite the existing use profile memory
    key = "user_memory"
    store.put(namespace, key, new_memory)


def should_summarize(state: SummarizedMessagesState):
    """Return the next node to execute."""

    messages = state["messages"]

    # If there are more than six messages, then we summarize the conversation
    if len(messages) > 6:
        return "summarize"

    # Otherwise we can just end
    return END


def tool_node(state: SummarizedMessagesState, config: RunnableConfig):
    """tools are specific to the league_id"""
    league = League(config['configurable']['league_id'])
    tools_by_name = {t.name: t for t in get_tools(league)}

    result = []
    for tool_call in state["messages"][-1].tool_calls:
        tool = tools_by_name[tool_call["name"]]
        observation = tool.invoke(tool_call["args"])
        result.append(ToolMessage(content=observation, tool_call_id=tool_call["id"]))
    return {"messages": result}

# Graph
builder = StateGraph(MessagesState, config_schema=Configuration)

# Define nodes: these do the work
builder.add_node("assistant", assistant)
builder.add_node("tools", tool_node)
builder.add_node("write_memory", write_memory)

# Define edges: these determine how the control flow moves
builder.add_edge(START, "assistant")
builder.add_conditional_edges(
    "assistant",
    # If the latest message (result) from assistant is a tool call -> tools_condition routes to tools
    # If the latest message (result) from assistant is a not a tool call -> tools_condition routes to END
    tools_condition,
)
builder.add_edge("tools", "assistant")
builder.add_edge("tools", "write_memory")
builder.add_edge("write_memory", END)
# Store for long-term (across-thread) memory
across_thread_memory = InMemoryStore()

# Checkpointer for short-term (within-thread) memory
within_thread_memory = MemorySaver()

react_graph = builder.compile(checkpointer=within_thread_memory, store=across_thread_memory)
