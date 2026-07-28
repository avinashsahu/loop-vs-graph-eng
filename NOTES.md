# loop vs graph — notes

## loop_agent.py
- state: plain local vars in while-loop
- control flow: if/else inline, exit via break
- pro: short, easy read for linear retry
- con: add branch (e.g. "needs clarification" path) → nested ifs pile up fast

## graph_agent.py
- state: explicit dict passed node to node
- control flow: each node returns (next_node_name, state) — transitions are data, not code
- pro: add new node/edge without touching existing nodes; can draw as diagram; easy to log/trace which node ran
- con: more boilerplate for something this simple (4 nodes for 1 retry loop)

## when to pick which
- loop: task is basically linear, retry/backoff only, no real branching → loop_agent style
- graph: multiple paths, cycles that aren't just "retry same step", need to visualize/debug flow, or plan for parallel branches later → graph_agent style

## next (optional, costs real tokens)
- set USE_REAL_LLM=1 + ANTHROPIC_API_KEY, swap mode="answer" prompt for real question, watch it in real: 1 real completion each is enough.
