"""Compare chat models on full answers (player role -> rules only, no spoilers).

    python -m eval.answer_eval --models qwen3.5:latest,qwen3:4b-instruct
Prints each answer with timing and whether it cited its passages.
"""
import argparse
import json
import re

from app import answer

QUESTIONS = [
    "Can I make an opportunity attack against a creature that is grappled?",
    "How much does cure wounds heal at 3rd level?",
    "What happens when I drop to 0 hit points?",
    "How does concentration work if I take 22 damage?",
    "What is the airspeed velocity of an unladen swallow?",   # should say "not found"
]


def run(model: str, q: str) -> dict:
    text, meta = "", {}
    for line in answer.answer_stream(q, role="player", model=model):
        ev = json.loads(line)
        if ev["type"] == "token":
            text += ev["text"]
        elif ev["type"] in ("done", "error"):
            meta = ev
    return {"text": text, **meta}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="qwen3.5:latest")
    args = ap.parse_args(argv)
    for model in args.models.split(","):
        print(f"\n{'=' * 20} {model}")
        run(model, "warm up")                        # load the model first
        for q in QUESTIONS:
            r = run(model, q)
            cites = len(re.findall(r"\[\d+\]", r["text"]))
            print(f"\nQ: {q}\n   first token {r.get('first_token_s')}s, total {r.get('total_s')}s, "
                  f"{len(r['text'])} chars, {cites} citations {r.get('message', '')}")
            print("   " + r["text"].strip().replace("\n", "\n   "))


if __name__ == "__main__":
    main()
