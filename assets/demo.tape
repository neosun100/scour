# Scour demo recording — reproduce with:  vhs assets/demo.tape   (run from repo root)
# Requires: a .venv with the package installed + a .env (gateway URL + AWS creds).
Output assets/demo.gif

Set Shell "bash"
Set FontSize 16
Set Width 1100
Set Height 440
Set Padding 18
Set Theme "Dracula"

Hide
Type "source .venv/bin/activate && clear"
Enter
Sleep 2s
Show

Type "python examples/demo.py"
Sleep 800ms
Enter
Sleep 16s
Sleep 2s
