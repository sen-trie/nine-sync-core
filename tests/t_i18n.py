# The compiled UI dictionary: plain Python, no Blender. It is read with ast, since the module imports bpy.
import ast
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
I18N = os.path.join(ROOT, "i18n")
LOCALES = {"ja_JP", "zh_HANS"}
PLACEHOLDER = re.compile(r"%(?:\([A-Za-z_]\w*\))?[-#0 +]*(?:\*|\d+)?(?:\.(?:\*|\d+))?[sdifrxXeEgGcoa]")

failed = []


def check(label, ok, detail=""):
    print("%s %s%s" % ("ok  " if ok else "FAIL", label, " | %s" % (detail,) if detail != "" else ""))
    if not ok:
        failed.append(label)


def main():
    check("i18n is a package", os.path.isfile(os.path.join(I18N, "__init__.py")))
    with open(os.path.join(I18N, "translations.py"), encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), "translations.py")
    functions = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    check("register and unregister are defined", {"register", "unregister"} <= functions, sorted(functions))
    table = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "translations_dict" for t in node.targets):
            table = ast.literal_eval(node.value)
    check("translations_dict is a dict", isinstance(table, dict))
    table = table or {}
    check("only known locales", set(table) <= LOCALES, sorted(set(table) - LOCALES))
    bad = [
        (locale, key)
        for locale, entries in table.items()
        for key, text in entries.items()
        if not (isinstance(key, tuple) and len(key) == 2 and isinstance(text, str) and text)
    ]
    check("keys are (context, msgid) with text", not bad, bad[:3])
    mismatched = [
        (locale, key[1])
        for locale, entries in table.items()
        for key, text in entries.items()
        if sorted(PLACEHOLDER.findall(key[1])) != sorted(PLACEHOLDER.findall(text))
    ]
    check("placeholders match their source", not mismatched, mismatched[:3])
    with open(os.path.join(ROOT, "blender_manifest.toml"), encoding="utf-8") as handle:
        manifest = handle.read()
    check("catalog sources are kept out of the build", '"*.po"' in manifest and '"*.pot"' in manifest)


if __name__ == "__main__":
    main()
    print("FAILED: %s" % ", ".join(failed) if failed else "all ok")
    sys.exit(1 if failed else 0)
