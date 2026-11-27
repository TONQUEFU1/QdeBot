import os, ast, sys

SKIP = {".git","__pycache__",".venv","venv","env","node_modules","dist","build"}
issues_total = 0

for dp, dn, fn in os.walk("."):
    dn[:] = [d for d in dn if d not in SKIP]
    for f in fn:
        if not f.endswith(".py"): continue
        p = os.path.join(dp, f)
        try:
            src = open(p, encoding="utf-8", errors="ignore").read()
        except Exception as e:
            print(f"{p}: OKUNAMADI ({e})"); continue
        issues = []
        lines = src.splitlines()
        for i, line in enumerate(lines, 1):
            s = line.strip()
            if s == "except:" or s.startswith("except Exception: pass") or s == "pass" and i>1 and "except" in lines[i-2]:
                issues.append(f"  L{i}: çıplak/boş except -> {s}")
            if "TODO" in s or "FIXME" in s or "XXX" in s:
                issues.append(f"  L{i}: {s}")
            if s.startswith("print(") and "__main__" not in src[max(0,src.find(s)-200):src.find(s)]:
                issues.append(f"  L{i}: debug print? -> {s[:80]}")
            if "time.sleep(" in s and "async" in src:
                issues.append(f"  L{i}: async içinde blocking sleep -> {s[:80]}")
        # AST kontrolleri
        try:
            tree = ast.parse(src)
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    n = node.end_lineno - node.lineno
                    if n > 60:
                        issues.append(f"  L{node.lineno}: uzun fonksiyon '{node.name}' ({n} satır)")
                    if len(node.args.args) > 6:
                        issues.append(f"  L{node.lineno}: çok parametreli '{node.name}' ({len(node.args.args)})")
                if isinstance(node, ast.ExceptHandler) and node.type is None:
                    issues.append(f"  L{node.lineno}: bare except")
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("eval","exec"):
                    issues.append(f"  L{node.lineno}: eval/exec kullanımı")
        except SyntaxError as e:
            issues.append(f"  SYNTAX HATASI: {e}")
        if issues:
            issues_total += len(issues)
            print(f"\n{p}")
            print("\n".join(issues))

print(f"\n\n=== TOPLAM: {issues_total} uyarı ===")
