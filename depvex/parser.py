import ast
import sys

STD_LIB = set(getattr(sys, "stdlib_module_names", set())) | set(sys.builtin_module_names)

def extract_imports(code: str):
    tree = ast.parse(code)
    imports = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for n in node.names:
                name = n.name.split(".")[0]
                if name not in STD_LIB:
                    imports.add(name)

        if isinstance(node, ast.ImportFrom):
            if node.module:
                name = node.module.split(".")[0]
                if name not in STD_LIB:
                    imports.add(name)

    return list(imports)