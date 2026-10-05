"""Build docs/Team_Setup_Guide_Bloomberg_Python_VSCode.pdf.

Needs reportlab, which the project itself doesn't use:  .venv\\Scripts\\python.exe -m pip install reportlab
Uses Windows' Segoe UI and Consolas fonts.
"""
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.fonts import addMapping
from reportlab.platypus.flowables import HRFlowable
from reportlab.platypus import (BaseDocTemplate, Frame, KeepTogether, ListFlowable, ListItem, NextPageTemplate,
                                PageBreak, PageTemplate, Paragraph, Preformatted, Spacer, Table, TableStyle)

REPO = Path(__file__).resolve().parents[1]
OUT = Path(__file__).resolve().parent / "Team_Setup_Guide_Bloomberg_Python_VSCode.pdf"

F = r"C:\Windows\Fonts"
pdfmetrics.registerFont(TTFont("Sans", f"{F}\\segoeui.ttf"))
pdfmetrics.registerFont(TTFont("Sans-Bold", f"{F}\\segoeuib.ttf"))
pdfmetrics.registerFont(TTFont("Sans-Italic", f"{F}\\segoeuii.ttf"))
pdfmetrics.registerFont(TTFont("Sans-Semi", f"{F}\\seguisb.ttf"))
pdfmetrics.registerFont(TTFont("Sans-Light", f"{F}\\segoeuisl.ttf"))
pdfmetrics.registerFont(TTFont("Mono", f"{F}\\consola.ttf"))
pdfmetrics.registerFont(TTFont("Mono-Bold", f"{F}\\consolab.ttf"))
addMapping("Sans", 0, 0, "Sans"); addMapping("Sans", 1, 0, "Sans-Bold")
addMapping("Sans", 0, 1, "Sans-Italic"); addMapping("Sans", 1, 1, "Sans-Bold")
addMapping("Mono", 0, 0, "Mono"); addMapping("Mono", 1, 0, "Mono-Bold")
addMapping("Mono", 0, 1, "Mono"); addMapping("Mono", 1, 1, "Mono-Bold")

INK = colors.HexColor("#1F2933")
MUTED = colors.HexColor("#5B6770")
ACCENT = colors.HexColor("#1D4ED8")
RULE = colors.HexColor("#D9DEE3")
CODE_BG = colors.HexColor("#F3F5F7")
CALLOUT = {
    "note": (colors.HexColor("#2563EB"), colors.HexColor("#EEF4FF"), "Note"),
    "warn": (colors.HexColor("#C2410C"), colors.HexColor("#FFF5EC"), "Important"),
    "check": (colors.HexColor("#15803D"), colors.HexColor("#EEFBF2"), "Check"),
}

body = ParagraphStyle("body", fontName="Sans", fontSize=10, leading=14.5, textColor=INK, spaceAfter=6)
small = ParagraphStyle("small", parent=body, fontSize=8.5, leading=12, textColor=MUTED)
h1 = ParagraphStyle("h1", fontName="Sans-Bold", fontSize=17, leading=22, textColor=INK, spaceBefore=4, spaceAfter=4,
                    keepWithNext=1)
h2 = ParagraphStyle("h2", fontName="Sans-Semi", fontSize=12, leading=16, textColor=INK, spaceBefore=12, spaceAfter=4,
                    keepWithNext=1)
kicker = ParagraphStyle("kicker", fontName="Sans-Semi", fontSize=9, leading=12, textColor=ACCENT, spaceAfter=0,
                        keepWithNext=1)
lead = ParagraphStyle("lead", parent=body, fontSize=10.5, leading=15.5, textColor=MUTED, spaceAfter=10, keepWithNext=1)
body_kwn = ParagraphStyle("body_kwn", parent=body, keepWithNext=1)
code = ParagraphStyle("code", fontName="Mono", fontSize=8.6, leading=12, textColor=INK)
cell = ParagraphStyle("cell", parent=body, fontSize=8.8, leading=12, spaceAfter=0)
cell_b = ParagraphStyle("cellb", parent=cell, fontName="Sans-Semi")
cell_mono = ParagraphStyle("cellm", parent=cell, fontName="Mono", fontSize=8.2)

W = A4[0] - 2 * 20 * mm


def c(t):
    """Inline code span."""
    return f'<font face="Mono" size="9" color="#0B3A8C">{t}</font>'


def P(text, style=body):
    # a sentence introducing a command or list stays on the same page as it
    if style is body and text.rstrip().endswith(":"):
        style = body_kwn
    return Paragraph(text, style)


def code_block(text):
    pre = Preformatted(text.strip("\n"), code)
    t = Table([[pre]], colWidths=[W])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), CODE_BG),
        ("BOX", (0, 0), (-1, -1), 0.5, RULE),
        ("LEFTPADDING", (0, 0), (-1, -1), 9), ("RIGHTPADDING", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    return [t, Spacer(1, 7)]


def callout(kind, text):
    edge, bg, label = CALLOUT[kind]
    p = Paragraph(f'<font face="Sans-Bold" color="{edge.hexval().replace("0x", "#")}">{label}.</font> {text}',
                  ParagraphStyle("co", parent=body, spaceAfter=0, fontSize=9.4, leading=13.5))
    t = Table([[p]], colWidths=[W])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), bg),
        ("LINEBEFORE", (0, 0), (0, -1), 3, edge),
        ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    if text.rstrip().endswith(":"):
        return [KeepTogether([t, Spacer(1, 8)] + _pending_code.pop())] if _pending_code else [t, Spacer(1, 8)]
    return [t, Spacer(1, 8)]


_pending_code = []


def callout_with_code(kind, text, code_text):
    _pending_code.append(code_block(code_text))
    return callout(kind, text)


def bullets(items, numbered=False):
    lf = ListFlowable(
        [ListItem(P(i, ParagraphStyle("li", parent=body, spaceAfter=2)), leftIndent=14) for i in items],
        bulletType="1" if numbered else "bullet", start="1" if numbered else None,
        bulletFontName="Sans-Semi" if numbered else "Sans", bulletFontSize=9.5,
        bulletColor=ACCENT if numbered else MUTED, leftIndent=14, bulletDedent=12,
        **({} if numbered else {"bulletChar": "•"}))
    return [lf, Spacer(1, 4)]


def table(rows, widths, header=True, mono_cols=()):
    data = []
    for r, row in enumerate(rows):
        out = []
        for i, v in enumerate(row):
            st = cell_b if (header and r == 0) else (cell_mono if i in mono_cols else cell)
            out.append(Paragraph(v, st))
        data.append(out)
    t = Table(data, colWidths=[W * w for w in widths], repeatRows=1 if header else 0)
    style = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, RULE),
        ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    if header:
        style += [("LINEBELOW", (0, 0), (-1, 0), 1, INK)]
    t.setStyle(TableStyle(style))
    return [t, Spacer(1, 9)]


def step(n, title, intro=None):
    out = []
    if True:
        rule = HRFlowable(width="100%", thickness=0.6, color=RULE, spaceBefore=10, spaceAfter=12)
        rule.keepWithNext = 1
        out.append(rule)
    out += [P(f"STEP {n}", kicker), P(title, h1)]
    if intro:
        out.append(P(intro, lead))
    return out


# --------------------------------------------------------------------------- page templates

def on_page(canv, doc):
    canv.saveState()
    canv.setStrokeColor(RULE)
    canv.setLineWidth(0.5)
    canv.line(20 * mm, 16 * mm, A4[0] - 20 * mm, 16 * mm)
    canv.setFont("Sans", 8)
    canv.setFillColor(MUTED)
    canv.drawString(20 * mm, 11 * mm, "Team setup guide · Bloomberg API + Python + VS Code")
    canv.drawRightString(A4[0] - 20 * mm, 11 * mm, f"Page {doc.page}")
    canv.restoreState()


def on_cover(canv, doc):
    canv.saveState()
    canv.setFillColor(ACCENT)
    canv.rect(0, A4[1] - 9 * mm, A4[0], 9 * mm, stroke=0, fill=1)
    canv.restoreState()


doc = BaseDocTemplate(str(OUT), pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm, topMargin=20 * mm,
                      bottomMargin=22 * mm, title="Team setup guide: Bloomberg API + Python + VS Code",
                      author="Bloomberg project team", subject="Replicating the reference development setup")
frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="f")
doc.addPageTemplates([PageTemplate("cover", [frame], onPage=on_cover),
                      PageTemplate("normal", [frame], onPage=on_page)])

class Story(list):
    def __iadd__(self, x):
        if isinstance(x, list):
            self.extend(x)
        else:
            self.append(x)
        return self


s = Story()

# --------------------------------------------------------------------------- cover
s += [Spacer(1, 6 * mm), P("TEAM SETUP GUIDE", kicker),
      P("Bloomberg API, Python and VS Code", ParagraphStyle("t", parent=h1, fontSize=26, leading=32)),
      Spacer(1, 4),
      P("Follow these steps to reproduce the exact development setup used for the project: the "
        "Bloomberg Terminal Desktop API, Python 3.13 with the same packages, and VS Code configured to use "
        "them. At the end, a check script confirms that your machine matches.", lead),
      Spacer(1, 6)]
s += table([
    ["Component", "Version on the reference laptop", "Where it lives"],
    ["Windows", "Windows 11 Education, 64-bit", "-"],
    ["Bloomberg Terminal", "Terminal installer 2.0, Desktop API (bbcomm)", c("C:\\blp")],
    ["Python", "3.13.16, 64-bit, per-user install", c("%LOCALAPPDATA%\\Programs\\Python\\Python313")],
    ["pip", "26.2.1 (inside the project environment)", c(".venv")],
    ["blpapi (Python SDK)", "3.26.9.1, from Bloomberg's package index", c(".venv")],
    ["VS Code", "1.140.0, User installer, 64-bit", c("%LOCALAPPDATA%\\Programs\\Microsoft VS Code")],
    ["Project environment", "Virtual environment named .venv in the project folder", c("&lt;project&gt;\\.venv")],
], [0.24, 0.42, 0.34])
s += [P("Contents", h2)]
s += table([
    ["Step", "What you do", "Time"],
    ["1", "Install and check the Bloomberg Terminal and its API", "10 min"],
    ["2", "Install Python 3.13 (64-bit)", "5 min"],
    ["3", "Install VS Code and its Python extensions", "5 min"],
    ["4", "Get the project code from GitHub", "2 min"],
    ["5", "Create the Python environment and install the packages", "10 min"],
    ["6", "Connect VS Code to the environment", "3 min"],
    ["7", "Verify the whole setup", "3 min"],
    ["8", "Set up your account for the shared data collector", "5 min"],
    ["A-C", "Daily use, troubleshooting, exact package versions", "reference"],
], [0.1, 0.75, 0.15])
s += [NextPageTemplate("normal"), PageBreak()]

s += [P("BEFORE YOU START", kicker), P("What you need", h1)]
s += bullets([
    "A Windows 10 or 11 PC, 64-bit. The Bloomberg Desktop API exists only on Windows.",
    "<b>Your own</b> Bloomberg Terminal login. On a library or lab PC the Terminal is usually already installed.",
    "Internet access, and about 45 minutes. Administrator rights are <b>not</b> needed: everything installs "
    "for your Windows user only.",
    "Access to the team's GitHub repository (code) and to the shared OneDrive folder (data).",
])
s += callout("warn", "The Python code can only reach Bloomberg on the <b>same PC</b> where the Terminal is "
                     "running and you are logged in. A laptop without the Terminal can run the analysis on "
                     "data already downloaded, but cannot download new data.")
s += callout("note", "Type the commands in this guide exactly as shown, in the VS Code terminal or PowerShell. "
                     f"Replace {c('&lt;you&gt;')} with your Windows user name wherever it appears in a path.")

# --------------------------------------------------------------------------- step 1
s += step(1, "Bloomberg Terminal and the Desktop API",
          "Python talks to Bloomberg through a small background program called bbcomm, which the Terminal "
          "starts automatically. It listens on your PC at port 8194. If the Terminal works and you are "
          "logged in, the API normally works too.")
s += [P("1.1  Install the Terminal (skip on lab PCs where it is already installed)", h2)]
s += bullets([
    "Go to <b>bloomberg.com/professional/support/software-updates</b> and download "
    "<b>Bloomberg Terminal - New Installation</b>.",
    f"Run the installer and keep the default location {c('C:\\blp')}. Every Python setup in this guide expects it there.",
    "Restart the PC if the installer asks you to.",
], numbered=True)
s += [P("1.2  Log in and keep the Terminal open", h2)]
s += bullets([
    "Start the Terminal and log in with <b>your own</b> username, password and B-Unit or mobile authentication.",
    "Leave it logged in whenever you run Python scripts. If you log out, or log in on another PC, the "
    "scripts lose their connection.",
], numbered=True)
s += [P("1.3  Check that the API is running", h2)]
s += bullets([
    f"Open Task Manager (Ctrl+Shift+Esc), tab <b>Details</b>, and look for {c('bbcomm.exe')}. On the "
    f"reference laptop it runs from {c('C:\\blp\\DAPI\\bbcomm.exe')}.",
    f"In the Terminal you can type {c('DAPI')} then press <b>GO</b> (Enter) for Bloomberg's own API help page.",
    "To check the port, open PowerShell (Windows key, type PowerShell) and run:",
])
s += code_block("""
Test-NetConnection 127.0.0.1 -Port 8194 -InformationLevel Quiet
""")
s += P(f"It should print {c('True')}. If it prints {c('False')}, see Appendix B, problem 6.")
s += callout("note", "Bloomberg enforces daily and monthly download limits <b>per login</b>. Each teammate has "
                     "their own allowance, which is why the project collector can share the work between "
                     "several accounts (Step 8).")

# --------------------------------------------------------------------------- step 2
s += step(2, "Install Python 3.13 (64-bit)",
          "The project uses Python 3.13. Other versions may fail to install some packages or behave "
          "differently, so install 3.13 even if you already have another Python.")
s += [P("2.1  Download", h2)]
s += bullets([
    "Go to <b>python.org/downloads/windows</b>.",
    "Under the latest <b>Python 3.13.x</b> release, choose <b>Windows installer (64-bit)</b>. Do not choose "
    "32-bit or ARM64: blpapi only works with 64-bit Intel/AMD Python.",
], numbered=True)
s += [P("2.2  Run the installer with these options", h2)]
s += bullets([
    "On the first screen, tick <b>Add python.exe to PATH</b>.",
    "Leave <b>Use admin privileges when installing py.exe</b> unticked (no admin rights needed).",
    "Click <b>Install Now</b>. Python goes to "
    f"{c('C:\\Users\\&lt;you&gt;\\AppData\\Local\\Programs\\Python\\Python313')}, like on the reference laptop.",
    "At the end, if offered, click <b>Disable path length limit</b>.",
], numbered=True)
s += [P("2.3  Check the installation", h2)]
s += P("Close any open PowerShell window, open a new one, and run:")
s += code_block("""
py -0p
python --version
""")
s += P(f"{c('py -0p')} lists the Pythons installed. You should see a line starting with {c('-V:3.13')}. "
       f"{c('python --version')} should print {c('Python 3.13.x')}.")
s += callout("warn", f"If {c('python')} opens the Microsoft Store instead, Windows' shortcut is in the way. Open "
                     "<b>Settings > Apps > Advanced app settings > App execution aliases</b> and switch off "
                     f"{c('python.exe')} and {c('python3.exe')}. Then open a new PowerShell window.")

# --------------------------------------------------------------------------- step 3
s += step(3, "Install VS Code and the Python extensions",
          "VS Code is the editor the team uses. Four Microsoft extensions give it Python support.")
s += [P("3.1  Install VS Code", h2)]
s += bullets([
    "Go to <b>code.visualstudio.com</b> and download the Windows <b>User Installer, x64</b> "
    "(version 1.140 or newer).",
    "During installation, keep <b>Add to PATH</b> ticked, and tick <b>Add 'Open with Code' action</b> "
    "for files and folders.",
    "Finish, then start VS Code.",
], numbered=True)
s += [P("3.2  Install the extensions", h2)]
s += P("Open the Extensions panel (Ctrl+Shift+X), search for each name below and click <b>Install</b>. "
       "Or install all of them at once from a <b>new</b> PowerShell window:")
s += code_block("""
code --install-extension ms-python.python
code --install-extension ms-python.vscode-pylance
code --install-extension ms-python.debugpy
code --install-extension ms-python.vscode-python-envs
""")
s += table([
    ["Extension", "Identifier", "Version on reference laptop", "Needed?"],
    ["Python", "ms-python.python", "2026.6.0", "Required"],
    ["Pylance", "ms-python.vscode-pylance", "2026.4.1", "Required"],
    ["Python Debugger", "ms-python.debugpy", "2026.6.0", "Required"],
    ["Python Environments", "ms-python.vscode-python-envs", "1.38.0", "Required"],
    ["Claude Code", "anthropic.claude-code", "2.1.289", "Optional (AI assistant)"],
    ["ChatGPT / Codex", "openai.chatgpt, openai.codex-audio", "26.930.51102", "Optional (AI assistant)"],
], [0.2, 0.36, 0.22, 0.22], mono_cols=(1,))
s += callout("check", f"Run {c('code --list-extensions')} in PowerShell. The four {c('ms-python')} entries "
                      "should be listed.")

# --------------------------------------------------------------------------- step 4
s += step(4, "Get the project code",
          "The code lives in the team's GitHub repository. Downloaded data is never in the repository: it "
          "stays in the shared data folder (Step 8).")
s += P("If Git is installed (<b>git-scm.com</b>, default options), open PowerShell and run:")
s += code_block("""
cd $HOME\\Documents
git clone https://github.com/tejoker/AFIB-M2.git
""")
s += bullets([
    f"Without Git: on the repository page click <b>Code > Download ZIP</b> and extract it into "
    f"{c('C:\\Users\\&lt;you&gt;\\Documents')}. Git is better because {c('git pull')} gets the team's updates.",
    "Keep the path simple, without special characters or spaces.",
    f"<b>Do not copy someone else's {c('.venv')} folder.</b> A virtual environment only works on the PC "
    "where it was created. You create your own in Step 5.",
], numbered=True)
s += P("The repository is organised like this:")
s += table([
    ["File or folder", "What it is"],
    [c("requirements.txt"), "Python packages and exact versions"],
    [c("requirements-bloomberg.txt"), "blpapi, installed from Bloomberg's own package index"],
    [c("verify_setup.py"), "Checks your setup (Step 7)"],
    [c(".vscode\\settings.json"), "Tells VS Code to use the project's .venv automatically"],
    [c("bloomberg\\"), "Shared Bloomberg collector bbg_collect.py (Step 8), quote example, CME data probe"],
    [c("research\\credit_ratings\\"), "IG/HY crossover screen and regression data checks"],
    [c("research\\russell_liquidity\\"), "Russell membership and liquidity research"],
    [c("cqg\\"), "CQG Web API connection tests (credentials template included)"],
    [c("cme_challenge\\"), "CME University Trading Challenge research (commodity history)"],
    [c("tests\\"), "Offline tests, no Bloomberg access needed"],
    [c("tools\\, vendor\\, docs\\"), "Program runner, CQG protocol code, this guide"],
    [c("data\\"), "Not in the repository: downloaded data, collector progress, team account list"],
], [0.36, 0.64])

# --------------------------------------------------------------------------- step 5
s += step(5, "Create the Python environment and install the packages",
          "Every package is installed in a virtual environment, a .venv folder inside the project. This "
          "keeps the project's exact versions separate from anything else on your PC.")
s += [P("5.1  Open the project in VS Code", h2)]
s += bullets([
    "In VS Code: <b>File > Open Folder...</b> and pick the project folder.",
    "If VS Code asks <b>Do you trust the authors of the files in this folder?</b>, click <b>Yes, I trust the authors</b>.",
    "Open the built-in terminal with <b>Ctrl+`</b> (the key left of 1) or <b>Terminal > New Terminal</b>. "
    "It opens PowerShell in the project folder.",
], numbered=True)
s += [P("5.2  Create the environment", h2)]
s += code_block("""
py -3.13 -m venv .venv
""")
s += P(f"This creates a {c('.venv')} folder using Python 3.13. It takes a few seconds and prints nothing.")
s += [P("5.3  Install the packages", h2)]
s += P("Run these three commands one after the other:")
s += code_block("""
.venv\\Scripts\\python.exe -m pip install --upgrade pip
.venv\\Scripts\\python.exe -m pip install -r requirements.txt
.venv\\Scripts\\python.exe -m pip install -r requirements-bloomberg.txt
""")
s += P(f"The second command installs pandas, numpy, pyarrow and the other packages at the team's exact "
       f"versions, plus everything they depend on. The third installs {c('blpapi 3.26.9.1')}. Each ends with "
       f"{c('Successfully installed ...')}. Together they take 2 to 5 minutes.")
s += callout("note", f"{c('blpapi')} is <b>not</b> on the normal Python package site (PyPI), which is why it has "
                     f"its own file: {c('requirements-bloomberg.txt')} points pip to Bloomberg's own package index.")
s += P("The blpapi package already includes the Bloomberg C++ library, so there is nothing else to download "
       "from Bloomberg's developer site.", small)
s += [P("5.4  Allow PowerShell to activate the environment (once per PC)", h2)]
s += P("VS Code activates the environment for you in new terminals. On many PCs, Windows blocks the "
       "activation script with the error <i>running scripts is disabled on this system</i>. To allow it for "
       "your user only, run this once and answer <b>Y</b>:")
s += code_block("""
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
""")
s += P(f"Then close the terminal and open a new one (Ctrl+`). The prompt should now start with {c('(.venv)')}. "
       f"If your PC does not allow this change, it doesn't matter: always run Python as "
       f"{c('.venv\\Scripts\\python.exe')}, as in this guide.")

# --------------------------------------------------------------------------- step 6
s += step(6, "Connect VS Code to the environment",
          "VS Code needs to know it should use the project's .venv, both to run scripts and for code "
          "completion. The project folder already contains the setting.")
s += P(f"The file {c('.vscode\\settings.json')} in the project folder contains exactly this:")
s += code_block("""
{
    "python.defaultInterpreterPath": "${workspaceFolder}\\\\.venv\\\\Scripts\\\\python.exe",
    "python.terminal.activateEnvironment": true
}
""")
s += bullets([
    "If the file is missing, create the folder <b>.vscode</b> in the project folder, and in it a file "
    "<b>settings.json</b> with the content above.",
    "Press <b>Ctrl+Shift+P</b>, type <b>Python: Select Interpreter</b> and choose the entry showing "
    f"{c('.venv')} and Python 3.13.16 (or your 3.13.x).",
    f"Check the bottom-right corner of VS Code: when a .py file is open, it should show "
    f"{c('3.13.x (.venv)')}.",
    f"Open {c('verify_setup.py')} and click the <b>Run</b> (triangle) button at the top right. The terminal "
    "should list the checks from Step 7.",
], numbered=True)

# --------------------------------------------------------------------------- step 7
s += step(7, "Verify the whole setup",
          "verify_setup.py checks Python, the packages and the connection to Bloomberg in one go. "
          "Run it with the Terminal open and logged in.")
s += code_block("""
.venv\\Scripts\\python.exe verify_setup.py
""")
s += P("Expected result:")
s += code_block("""
[OK] Python version - 3.13.16 (expected 3.13.x)
[OK] 64-bit Python
[OK] Running from the project .venv - ...\\AFIB-M2\\.venv\\Scripts\\python.exe
[OK] package numpy - 2.5.3
[OK] package pandas - 3.0.6
[OK] package pyarrow - 25.0.1
[OK] package openpyxl - 3.1.5
[OK] package XlsxWriter - 3.2.9
[OK] package matplotlib - 3.11.2
[OK] package protobuf - 7.36.2
[OK] package websockets - 17.2
[OK] package yfinance - 1.7.0
[OK] package blpapi - 3.26.9.1
[OK] import blpapi
[OK] Bloomberg API port 8194 (bbcomm)
[OK] Bloomberg session start
[OK] Service //blp/refdata

ALL CHECKS PASSED
""")
s += P("By default the check only connects to Bloomberg and downloads nothing, so it does not use your "
       "daily allowance. Once, to prove that data really comes through, run it with <b>--data</b>. That "
       "downloads a single value (the S&amp;P 500 last price):")
s += code_block("""
.venv\\Scripts\\python.exe verify_setup.py --data
""")
s += callout("check", f"If every line says {c('[OK]')}, your setup matches the reference laptop, down to each "
                      f"package version. Any {c('[FAIL]')} line points to the problem: see Appendix B.")
s += P("The offline tests check the project code itself, without Bloomberg. They take about a minute:")
s += code_block("""
.venv\\Scripts\\python.exe -m unittest discover -s tests
""")

# --------------------------------------------------------------------------- step 8
s += step(8, "Set up your account for the shared data collector",
          "bloomberg\\bbg_collect.py downloads the project dataset. Several teammates can run it, each with "
          "their own Bloomberg login: the work is split between accounts and nothing is downloaded twice.")
s += [P("8.1  Connect to the shared data folder", h2)]
s += P("All accounts must write to the <b>same</b> data folder, so they can see each other's progress. The team "
       "keeps it in a shared OneDrive/SharePoint folder (never on GitHub). Once that folder is synced to your PC, "
       "tell the scripts where it is (adapt the path), then close and reopen VS Code:")
s += code_block("""
[Environment]::SetEnvironmentVariable("BBG_DATA_DIR",
    "C:\\Users\\<you>\\OneDrive - <University>\\AFIB-data", "User")
""")
s += P(f"Without this setting the scripts use the {c('data')} folder inside the repository, which only you see.",
       small)
s += [P("8.2  Find your account name", h2)]
s += P("The collector identifies you by your Windows user name. In the VS Code terminal run:")
s += code_block("""
$env:USERNAME
""")
s += [P("8.3  Add it to the team account list (once, for the whole team)", h2)]
s += P(f"The file {c('accounts.json')} in the shared data folder lists every teammate's account, for example:")
s += code_block("""
{"accounts": ["alice", "bob", "carol"]}
""")
s += callout("warn", "Agree on the list once, before anyone collects, and <b>never change the order "
                     "afterwards</b>: the order decides which part of the work each account owns. If several "
                     "people use PCs with the same Windows user name (shared lab logins), add "
                     f"{c('--account yourname')} to every collector command instead.")
s += [P("8.4  Check progress, then run", h2)]
s += code_block("""
.venv\\Scripts\\python.exe bloomberg\\bbg_collect.py --status
.venv\\Scripts\\python.exe bloomberg\\bbg_collect.py
""")
s += P(f"{c('--status')} shows progress per dataset and per account, without using Bloomberg. The second "
       "command collects whatever is still missing, starting with the data the regression needs. It saves "
       "after every batch, so you can stop it at any time (Ctrl+C) and run it again later.")
s += bullets([
    f"When Bloomberg's daily limit for your login is reached, the collector stops by itself with "
    f"{c('Daily capacity reached')} and refuses to run for 12 hours. That's normal: run it again the next day, "
    "while the other accounts carry on.",
    f"If the data folder cannot be shared, each person collects into their own {c('data')} folder and you copy "
    "them together later: the files never overwrite each other, but some batches may be fetched twice.",
])
s += callout("warn", "Bloomberg licenses its data to each individual user. Keep downloaded data private to the "
                     "team (never in a public repository or website), and check with whoever manages your "
                     "Terminals that pooling several logins for one dataset is allowed.")

# --------------------------------------------------------------------------- appendix A
s += [PageBreak(), P("APPENDIX A", kicker), P("Daily use cheat sheet", h1)]
s += P("Run from the VS Code terminal in the project folder, with the Terminal logged in.", lead)
s += table([
    ["Task", "Command"],
    ["Check the setup", c(".venv\\Scripts\\python.exe verify_setup.py")],
    ["Get the team's latest code", c("git pull")],
    ["Collector progress, all accounts", c(".venv\\Scripts\\python.exe bloomberg\\bbg_collect.py --status")],
    ["Continue collecting", c(".venv\\Scripts\\python.exe bloomberg\\bbg_collect.py")],
    ["Collect only what the regression needs", c(".venv\\Scripts\\python.exe bloomberg\\bbg_collect.py --mandatory-only")],
    ["Small test run (50 requests)", c(".venv\\Scripts\\python.exe bloomberg\\bbg_collect.py --max-requests 50")],
    ["Rebuild the data summary.xlsx", c(".venv\\Scripts\\python.exe bloomberg\\bbg_collect.py --summary-only")],
    ["Check data for the regression", c(".venv\\Scripts\\python.exe research\\credit_ratings\\check_regression_data.py")],
    ["Simple quote example", c(".venv\\Scripts\\python.exe bloomberg\\query.py")],
    ["Run the offline tests", c(".venv\\Scripts\\python.exe -m unittest discover -s tests")],
    ["Add a package (tell the team)", c(".venv\\Scripts\\python.exe -m pip install name") + " then add it to requirements.txt"],
], [0.36, 0.64])
s += P("Loading collected data in your own scripts", h2)
s += code_block("""
import sys
sys.path.insert(0, "bloomberg")              # run from the repository root
from bbg_collect import load_dataset, latest_snapshot

ratings = latest_snapshot()                    # latest ratings + reference data per company
annual = load_dataset("fundamentals_annual")   # one row per company and fiscal year
prices = load_dataset("prices_daily", columns=["ticker", "date", "PX_LAST"])
""")

# --------------------------------------------------------------------------- appendix B
s += [PageBreak(), P("APPENDIX B", kicker), P("Troubleshooting", h1)]
s += P("Find the message or symptom you see in the left column.", lead)
s += table([
    ["#", "Symptom", "Cause and fix"],
    ["1", f"{c('python')} opens the Microsoft Store, or prints nothing",
     "Windows app alias. Settings > Apps > Advanced app settings > App execution aliases: switch off "
     "python.exe and python3.exe. Open a new terminal."],
    ["2", f"{c('py')} is not recognized",
     "Python was installed without the launcher. Rerun the Python installer, choose Modify, tick "
     "py launcher. Or use the full path: %LOCALAPPDATA%\\Programs\\Python\\Python313\\python.exe -m venv .venv"],
    ["3", f"{c('py -3.13')}: <i>No suitable Python runtime found</i>",
     "Python 3.13 isn't installed (you may have another version). Install 3.13 64-bit (Step 2)."],
    ["4", f"<i>No matching distribution found for blpapi</i>",
     "blpapi is only on Bloomberg's index: install it with requirements-bloomberg.txt (Step 5.3), not "
     "requirements.txt. On restricted networks, try another network (e.g. eduroam or a phone hotspot)."],
    ["5", f"<i>ImportError: DLL load failed</i> when importing blpapi",
     "32-bit or ARM Python. Uninstall it, install Python 3.13 <b>64-bit</b>, delete the .venv folder and redo Step 5."],
    ["6", f"verify_setup: {c('[FAIL] Bloomberg API port 8194')}",
     "Terminal not started or not logged in, or bbcomm stopped. Log in to the Terminal; if it still fails, "
     "close the Terminal completely, run C:\\blp\\DAPI\\bbcomm.exe, then start the Terminal again."],
    ["7", f"{c('[FAIL] Bloomberg session start')} with the port OK",
     "Login not finished (B-Unit/2FA pending) or the Terminal is logged in on another PC. Finish logging "
     "in on this PC and try again."],
    ["8", f"<i>Daily capacity reached</i> or <i>Monthly capacity reached</i>",
     "Your login's Bloomberg download limit. Nothing is broken: wait until the next day (monthly: the next "
     "month). The collector saves progress and resumes by itself."],
    ["9", "<i>running scripts is disabled on this system</i>",
     "PowerShell's execution policy. Run the Set-ExecutionPolicy command in Step 5.4, or ignore it and "
     "call .venv\\Scripts\\python.exe directly."],
    ["10", "VS Code runs the wrong Python, or underlines \"import blpapi\" in yellow",
     "Ctrl+Shift+P > Python: Select Interpreter > choose the .venv entry. Check .vscode\\settings.json exists (Step 6)."],
    ["11", f"{c('code')} is not recognized in PowerShell",
     "VS Code was installed without Add to PATH. Reinstall with it ticked, or install the extensions from "
     "the Extensions panel instead."],
    ["12", f"<i>Account 'x' is not listed in accounts.json</i>",
     "Add your account name (Step 8.2) to accounts.json in the shared data folder, or pass --account with a "
     "listed name. If the file only lists you, BBG_DATA_DIR is probably not set (Step 8.1)."],
    ["13", "No module named 'WebAPI' or 'bbg_collect'",
     "Run scripts from the repository root, as written in this guide (e.g. bloomberg\\bbg_collect.py), "
     "and after a git pull, not from an old copy of the folder."],
    ["14", "pip fails building a package (error mentions a compiler or wheel)",
     "Usually a Python version other than 3.13, for which no ready-made package exists. Use Python 3.13 and recreate .venv."],
], [0.05, 0.33, 0.62])
s += P("Starting over cleanly", h2)
s += P("If the environment gets into a bad state, delete it and recreate it. This doesn't touch your code or data:")
s += code_block("""
Remove-Item -Recurse -Force .venv
py -3.13 -m venv .venv
.venv\\Scripts\\python.exe -m pip install --upgrade pip
.venv\\Scripts\\python.exe -m pip install -r requirements.txt
.venv\\Scripts\\python.exe -m pip install -r requirements-bloomberg.txt
""")

# --------------------------------------------------------------------------- appendix C
s += [PageBreak(), P("APPENDIX C", kicker), P("Exact package versions", h1)]
s += P(f"These are the packages the project uses directly, pinned in {c('requirements.txt')} and "
       f"{c('requirements-bloomberg.txt')}. pip installs exactly these versions and picks their own "
       f"dependencies automatically; {c('verify_setup.py')} checks every version below.", lead)
pkgs = [l.strip() for name in ("requirements.txt", "requirements-bloomberg.txt")
        for l in (REPO / name).read_text(encoding="utf-8").splitlines()
        if l.strip() and not l.startswith(("#", "--"))]
pairs = [p.split("==") for p in pkgs]
half = (len(pairs) + 1) // 2
rows = [["Package", "Version", "Package", "Version"]]
for i in range(half):
    a = pairs[i]
    b = pairs[i + half] if i + half < len(pairs) else ["", ""]
    rows.append([a[0], a[1], b[0], b[1]])
s += table(rows, [0.3, 0.2, 0.3, 0.2], mono_cols=(0, 1, 2, 3))
s += P("Other software on the reference laptop", h2)
s += table([
    ["Software", "Version", "Notes"],
    ["Git", "2.55.0", "Recommended: used to get the code and the team's updates (Step 4)."],
    ["reportlab", "any", "Only to rebuild this guide (docs\\make_setup_guide.py). Not needed by the project."],
    ["Windows PowerShell", "5.1", "Built into Windows. The commands in this guide are written for it."],
    ["Python 3.13 free-threaded build", "3.13.16", "Installed as an extra option of the Python installer. Not used by the project."],
], [0.3, 0.14, 0.56])

doc.build(s)
print("written", OUT)

