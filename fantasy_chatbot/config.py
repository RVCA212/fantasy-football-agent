import os

POSITIONS = ['QB', 'RB', 'WR', 'TE', 'K', 'DEF']

MODEL_ID_BEDROCK = os.environ.get(
    'MODEL_ID_BEDROCK', 'us.anthropic.claude-3-5-haiku-20241022-v1:0'
)
MODEL_ID_OPENAI = 'gpt-4o'

DEFAULT_USER = 'evandiewald'
DEFAULT_LEAGUE_ID = '1126330265028108288'
