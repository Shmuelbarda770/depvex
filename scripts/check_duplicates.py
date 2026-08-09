from collections import defaultdict
from pathlib import Path

INPUT_FILE = Path("/Users/shmuelbarda/Desktop/depvex/import_mapping_filtered.txt")

OUTPUT_FILE = Path("/Users/shmuelbarda/Desktop/depvex/duplicate_imports.txt")


imports = defaultdict(set)

with INPUT_FILE.open("r", encoding="utf-8") as f:
    for line in f:
        line = line.strip()

        if not line or line.startswith("#") or ":" not in line:
            continue

        import_name, package_name = line.split(":", 1)

        import_name = import_name.strip()
        package_name = package_name.strip()

        if import_name and package_name:
            imports[import_name].add(package_name)


duplicates = {import_name: sorted(packages) for import_name, packages in imports.items() if len(packages) > 1}


with OUTPUT_FILE.open("w", encoding="utf-8") as f:
    for import_name in sorted(duplicates):
        f.write(f"{import_name}:\n")

        for package in duplicates[import_name]:
            f.write(f"    {package}\n")

        f.write("\n")


print(f"Duplicate imports: {len(duplicates)}")
print(f"Output: {OUTPUT_FILE}")
