import sys

sys.path.insert(0, r"D:\Xenon")
from xenon.kernel.session_grants import GrantBook, grant_from_choice, ALLOW
from xenon.kernel.contracts import Capability

ws = "/repo"
b = GrantBook(ws)
b.add(
    grant_from_choice(
        ALLOW,
        capability=Capability.SHELL,
        targets=('python -c "print(123)"',),
        workspace=ws,
    )
)
variants = [
    'python -c "print(123)"',
    "'print(123)' | python -",
    'python "-c" "print(123)"',
    '"print(123)" | python',
    'python -c "print(123)"  # comment',
    'python -c "print(123)"',
]
print("按精确路径授权后，各变体是否命中：")
for v in variants:
    hit = bool(b.active(ws, Capability.SHELL, [v]))
    print(f"  {'HIT ' if hit else 'MISS'}  {v}")
