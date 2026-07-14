import ast
import sys


class ImportExtractor:
    def __init__(self) -> None:
        self.stdlib_modules = set(getattr(sys, "stdlib_module_names", set())) | set(sys.builtin_module_names)

    def _should_ignore(self, node: ast.AST, source_lines: list[str]) -> bool:
        line_number = getattr(node, "lineno", None)
        if line_number is None:
            return False

        line_text = source_lines[line_number - 1] if line_number - 1 < len(source_lines) else ""
        lowered = line_text.lower()
        return "ignore depvex" in lowered

    def extract_imports(self, code: str) -> list[str]:
        tree = ast.parse(code)
        source_lines = code.splitlines()
        imports = set()

        for node in ast.walk(tree):
            if self._should_ignore(node, source_lines):
                continue

            if isinstance(node, ast.Import):
                for import_name in node.names:
                    name = import_name.name.split(".")[0]
                    if name not in self.stdlib_modules:
                        imports.add(name)

            if isinstance(node, ast.ImportFrom) and node.module:
                name = node.module.split(".")[0]
                if name not in self.stdlib_modules:
                    imports.add(name)

        return sorted(imports)