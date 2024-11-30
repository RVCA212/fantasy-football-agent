FROM langchain/langgraph-api:3.11



ADD requirements.txt /deps/__outer_fantasy_chatbot/src/requirements.txt
RUN PYTHONDONTWRITEBYTECODE=1 pip install --no-cache-dir -c /api/constraints.txt -r /deps/__outer_fantasy_chatbot/src/requirements.txt

ADD . /deps/__outer_fantasy_chatbot/src
RUN set -ex && \
    for line in '[project]' \
                'name = "fantasy_chatbot"' \
                'version = "0.1"' \
                '[tool.setuptools.package-data]' \
                '"*" = ["**/*"]'; do \
        echo "$line" >> /deps/__outer_fantasy_chatbot/pyproject.toml; \
    done

RUN PYTHONDONTWRITEBYTECODE=1 pip install --no-cache-dir -c /api/constraints.txt -e /deps/*

ENV LANGSERVE_GRAPHS='{"chatbot": "/deps/__outer_fantasy_chatbot/src/chatbot.py:react_graph"}'

WORKDIR /deps/__outer_fantasy_chatbot/src