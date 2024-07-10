# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

#!/bin/bash 

DIR="xxx"

EVAL_DIR=$DIR"/eval"

cd $EVAL_DIR


# - environment variables start with "AGENT_" or "OPENAI_" are used for conversational agent
# - environment variables start with "SIMULATOR_" are used for user simulator 


#!/bin/bash  
DOMAIN="movie" \
AGENT_ENGINE="xxx" \
OPENAI_API_KEY="xxx" \
OPENAI_API_BASE="xxx" \
OPENAI_API_VERSION="xxx" \
OPENAI_API_TYPE="xxx" \
SIMULATOR_ENGINE="xxx" \
SIMULATOR_API_KEY="xxx" \
SIMULATOR_API_BASE="xxx" \
SIMULATOR_API_VERSION="xxx" \
SIMULATOR_API_TYPE="xxx" \
PYTHONPATH="$DIR" \
TOKENIZERS_PARALLELISM=false  \
python user_simulator.py \
    --timeout=60 \
    --bot_type="chat" \
    --enable_shorten=0 \
    --demo_mode="dynamic" \
    --demo_dir="../demonstration" \
    --num_demos=5 \
    --enable_reflection=1 \
    --plan_first=1 \
    --langchain=0 \
    --data="../resources/movie/simulator_test_data_900.jsonl" \
    --max_turns=5 \
    --agent="recbot"
