import json

INPUT_FILE = "top_level_results.jsonl"
OUTPUT_FILE = "errors_packages.txt"


def extract_errors():
    errors = []

    with open(INPUT_FILE, encoding="utf-8") as f:
        for line in f:
            try:
                data = json.loads(line)

                if "error" in data:
                    errors.append(
                        f"{data['package']} | {data['error']}"
                    )

            except json.JSONDecodeError:
                continue

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        for error in errors:
            f.write(error + "\n")

    print(f"Saved {len(errors)} errors to {OUTPUT_FILE}")


if __name__ == "__main__":
    extract_errors()