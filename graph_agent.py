from llm import call_llm
from logging_config import setup_logging

MAX_ITERS = 3

log = setup_logging("graph")


def node_answer(state):
    state["iters"] += 1
    state["answer"] = call_llm(f"Answer: {state['question']}", mode="answer")
    return "check", state


def node_check(state):
    state["verdict"] = call_llm(
        f"Check this answer: {state['answer']}\n"
        "Reply with exactly one word first: GOOD or BAD. "
        "Then, on the same line, a short reason.",
        mode="check",
    )
    log.info(
        "iter=%d answer=%r verdict=%r",
        state["iters"], state["answer"], state["verdict"],
    )
    if state["verdict"].startswith("GOOD"):
        return "done", state
    return "retry_guard", state


def node_retry_guard(state):
    if state["iters"] >= MAX_ITERS:
        log.warning("hit MAX_ITERS=%d without a GOOD verdict", MAX_ITERS)
        return "done", state
    return "answer", state


def node_done(state):
    return None, state


GRAPH = {
    "answer": node_answer,
    "check": node_check,
    "retry_guard": node_retry_guard,
    "done": node_done,
}


def run(question, start="answer"):
    state = {"question": question, "iters": 0, "answer": None, "verdict": None}
    node = start

    while node is not None:
        log.debug("node=%s", node)
        fn = GRAPH[node]
        node, state = fn(state)

    return state


if __name__ == "__main__":
    final_state = run("what is 2+2?")
    log.info("final: %r (after %d iters)", final_state["answer"], final_state["iters"])
