from llm import call_llm
from logging_config import setup_logging

MAX_ITERS = 3

log = setup_logging("loop")


def run(question):
    iters = 0
    answer = None

    while iters < MAX_ITERS:
        iters += 1
        answer = call_llm(f"Answer: {question}", mode="answer")
        verdict = call_llm(
            f"Check this answer: {answer}\n"
            "Reply with exactly one word first: GOOD or BAD. "
            "Then, on the same line, a short reason.",
            mode="check",
        )
        log.info("iter=%d answer=%r verdict=%r", iters, answer, verdict)

        if verdict.startswith("GOOD"):
            log.info("verdict GOOD, stopping at iter=%d", iters)
            break
    else:
        log.warning("hit MAX_ITERS=%d without a GOOD verdict", MAX_ITERS)

    return answer, iters


if __name__ == "__main__":
    final_answer, n = run("what is 2+2?")
    log.info("final: %r (after %d iters)", final_answer, n)
