def count_same_names(input_file):
    count = 0
    matches = []

    with open(input_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()

            if not line or ":" not in line:
                continue

            import_name, package_name = line.split(":", 1)

            normalized_import = import_name.replace("-", "_").lower()
            normalized_package = package_name.replace("-", "_").lower()

            if normalized_import == normalized_package:
                count += 1
                matches.append(line)

    print(f"Total matches: {count}")

    for item in matches[:50]:
        print(item)


count_same_names("import_mapping.txt")