import ast
import sys


class ImportExtractor:
    def __init__(self) -> None:
        self.stdlib_modules = set(getattr(sys, "stdlib_module_names", set())) | set(sys.builtin_module_names)

    def extract_imports(self, code: str) -> list[str]:
        tree = ast.parse(code)
        imports = set()

        for node in ast.walk(tree):
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
