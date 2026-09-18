from lib import *
import part_a, part_b, part_c, part_d
import subprocess

out = []
out += part_a.S + part_b.S + part_c.S + part_d.S

# ---- Appendix A: every scenario
out.append("\n# Appendix A. Every scenario script\n\nAll re-runnable. `99_reset` is stale and carries its own warning.\n\n")
scen = sorted(subprocess.check_output(["git", "ls-files", "ops/scenarios"], cwd=REPO, text=True).split())
for s in scen:
    out.append(file(s))

# ---- Appendix B: complete control plane source
out.append("\n# Appendix B. Complete source of the control plane\n\nFinal form at `" + commit("HEAD").split()[0] + "`. Every module, in dependency order.\n\n")
for m in ["control/config.py", "control/snow.py", "control/contracts.py", "control/register.py",
          "control/load.py", "control/lineage.py", "control/detect.py", "control/quality.py",
          "control/shield.py", "control/onboard.py", "control/agent.py", "control/cli.py", "control/ui.py"]:
    out.append(file(m))

text = "".join(out)
# collapse 3+ blank lines
import re
text = re.sub(r"\n{4,}", "\n\n\n", text)
dest = REPO + "/docs/IMPLEMENTATION_GUIDE.md"
open(dest, "w").write(text)
print(dest, len(text), "chars", text.count("\n"), "lines")
