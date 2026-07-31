#!/usr/bin/env python3
"""Self-test: every method of a PyObjC subclass has a usable selector prototype.

PyObjC turns most methods of an ``NSObject``/``NSView`` subclass into Objective-C
selectors *when the class is created* — i.e. at import time. The selector name
comes from the Python name (``objc._transform.default_selector``): underscores
become colons, except a single leading underscore, which is kept. So a private
helper named in camelCase with no other underscore is a trap::

    def _beginDmgDownload(self, res):   # selector '_beginDmgDownload' — 0 colons

That selector takes no arguments while the function wants one, so PyObjC raises
``BadPrototypeError`` and the *whole module* fails to import. Names with an
internal underscore (``_begin_dmg_download``, ``_open_url``) are left alone as
plain Python methods, and a trailing underscore spells the colon out
(``installUpdate_`` -> ``installUpdate:``).

Linux CI can't catch this by importing: PyObjC isn't installed, so the mac
modules fall back to ``NSObject = object`` and no selector is ever built. This
check is therefore static — it re-implements PyObjC's naming and arity rules
over the AST, and runs anywhere.

Run:  PYTHONPATH=. python3 scripts/mac_selector_test.py
"""
import ast
import keyword
import pathlib
import sys

# Methods PyObjC never turns into a selector.
_EXEMPT_DECORATORS = {"python_method", "staticmethod", "property"}


def default_selector(name):
    """Port of ``objc._transform.default_selector`` (PyObjC 12).

    Returns the Objective-C selector PyObjC would derive from *name*, or None
    when the name stays a plain Python method.
    """
    if name.endswith("__") and keyword.iskeyword(name[:-2]):
        name = name[:-2]
    if name.startswith("__") and name.endswith("__"):
        return None
    if "_" in name[1:] and not name.endswith("_"):
        return None
    value = name.replace("_", ":")
    if value.startswith(":"):            # a leading underscore is kept as-is
        value = "_" + value[1:]
    return value


def _decorator_names(fn):
    names = set()
    for d in fn.decorator_list:
        node = d.func if isinstance(d, ast.Call) else d
        names.add(node.attr if isinstance(node, ast.Attribute)
                  else getattr(node, "id", ""))
    return names


def check_method(fn):
    """Return a complaint string if PyObjC would reject *fn*, else None."""
    if _decorator_names(fn) & _EXEMPT_DECORATORS:
        return None
    selector = default_selector(fn.name)
    if selector is None:
        return None

    argcount = selector.count(":")       # args the selector passes, minus self
    args = fn.args
    pos = len(args.posonlyargs) + len(args.args)
    pos_default = len(args.defaults)

    kwonly = len(args.kwonlyargs)
    kwonly_default = sum(d is not None for d in args.kw_defaults)
    if kwonly - kwonly_default:
        return (f"selector {selector!r} cannot be called with the "
                f"{kwonly - kwonly_default} keyword-only argument(s) it declares")

    if args.vararg:                      # *args soaks up any arity
        return None
    if pos < argcount + 1 or pos - pos_default > argcount + 1:
        return (f"selector {selector!r} passes {argcount} argument(s) but the "
                f"method takes {pos - 1}")
    return None


def check_file(path):
    problems = []
    tree = ast.parse(path.read_text(), filename=str(path))
    for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
        bases = [b.id for b in cls.bases if isinstance(b, ast.Name)]
        if not any(b.startswith("NS") for b in bases):
            continue                     # not a PyObjC subclass
        for fn in cls.body:
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            complaint = check_method(fn)
            if complaint:
                problems.append(
                    f"{path}:{fn.lineno}: {cls.name}.{fn.name}() — {complaint}")
    return problems


def main():
    root = pathlib.Path(__file__).resolve().parent.parent / "clippy"
    files = sorted(root.rglob("*.py"))
    if not files:
        print(f"FAIL: no sources found under {root}")
        return 1

    # Guard the checker itself: the real 1.4.17 regression must be detected.
    regression = ast.parse(
        "class C(NSObject):\n"
        "    def _beginDmgDownload(self, res): pass\n").body[0].body[0]
    if check_method(regression) is None:
        print("FAIL: checker no longer detects the 1.4.17 _beginDmgDownload bug")
        return 1

    problems = [p for f in files for p in check_file(f)]
    if problems:
        print("FAIL: PyObjC would refuse to build these classes at import time:")
        for p in problems:
            print("  " + p)
        print("\nRename the method to snake_case (a plain Python method), add a "
              "trailing underscore per argument, or decorate it with "
              "@objc.python_method.")
        return 1

    classes = sum(
        1 for f in files for n in ast.walk(ast.parse(f.read_text()))
        if isinstance(n, ast.ClassDef)
        and any(isinstance(b, ast.Name) and b.id.startswith("NS") for b in n.bases))
    print(f"OK: selector prototypes valid across {classes} PyObjC classes "
          f"in {len(files)} sources")
    return 0


if __name__ == "__main__":
    sys.exit(main())
