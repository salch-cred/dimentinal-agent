import argparse
import sys
import json
from app import run_proof_pipeline

def main():
    parser = argparse.ArgumentParser(description="Proof-Carrying QA CLI")
    parser.add_argument("--question", required=True, type=str, help="Question to answer")
    parser.add_argument("--doc_path", required=True, type=str, help="Path to document")
    args = parser.parse_args()

    try:
        proof = run_proof_pipeline(args.question, args.doc_path)
        if proof.rejected:
            print(json.dumps({"rejected": True, "reason": proof.reason}))
            sys.exit(0)
        else:
            print(json.dumps({
                "rejected": False,
                "answer": proof.answer,
                "value": proof.normalized_value,
                "unit": proof.unit,
                "citations": proof.citations
            }))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)

if __name__ == "__main__":
    main()
