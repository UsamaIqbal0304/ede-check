#!/usr/bin/env python3
"""An EDE file checked against the parser Niagara actually ships.

    ./ede-check.py --template points.csv [--states states.csv]
    ./ede-check.py --check points.csv [--states states.csv] [--delimiter ';']
    (optional last argument: NIAGARA_HOME)

Why this exists. An EDE file that imports without an error is not the same as
an EDE file that imports correctly. bacnetEDE-wb.jar demands five columns and
quietly substitutes a property default for every later column that is blank, so
a file with no units and no state texts imports clean and lands as unitless
points with no enumeration. Nothing in the job log says so.

So this reads the rules out of the shipped jar instead of out of the EDE spec:

  * EdeCursor's recordProps array gives the column order, positionally;
  * parseRecord passes required=true only while the column index is below a
    constant this reads off the bytecode rather than assuming it is five;
  * skipLine gives the header rows that are skipped, by prefix on the
    uppercased first field, plus the comment marker;
  * parseRecordVal gives the per-column type rules - which columns go through
    BInteger.make, which through BDouble.make, which are "y" or not-"y", and
    which are escaped free text;
  * BBacnetEngineeringUnits gives the unit codes that resolve to a Niagara
    unit; a code outside that set fails the line, and the set is not
    contiguous;
  * EDEUtil gives the object types that become points at all, and the numeric,
    multi-state and boolean groups that decide how presentValueDefault is
    read.

--template writes a file whose columns are in the jar's order with the header
rows the jar skips. --check reports, per line, what the parser would throw and
what it would silently default. Nothing is installed, patched or sent anywhere.
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

def _find_javap(niagara_home=None):
    """javap from $JAVAP, then PATH, then the JDK Niagara ships, then Debian's."""
    cand = [os.environ.get("JAVAP"), shutil.which("javap")]
    for base in (os.environ.get("JAVA_HOME"), niagara_home):
        if base:
            cand += [str(Path(base) / "bin" / "javap"),
                     str(Path(base) / "jre" / "bin" / "javap")]
    cand.append("/usr/lib/jvm/java-8-openjdk-amd64/bin/javap")
    for c in cand:
        if c and Path(c).exists():
            return c
    return None


JAVAP = None  # resolved in main(), once NIAGARA_HOME is known
EDE = "com.tridium.bacnetEde"


def die(msg):
    print("ABORT " + msg, file=sys.stderr)
    sys.exit(2)


def unpack(jar, prefix):
    if not jar.exists():
        die(f"no {jar}")
    work = Path(tempfile.mkdtemp(prefix=prefix))
    with zipfile.ZipFile(jar) as z:
        z.extractall(work)
    return work


def disasm(work, cls, code=True):
    cmd = [JAVAP, "-p"] + (["-c"] if code else []) + ["-classpath", str(work), cls]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode or not r.stdout:
        die(f"javap failed on {cls}: {r.stderr.strip()[:200]}")
    return r.stdout


def method(dump, signature):
    m = re.search(re.escape(signature) + r";\n(.*?)(?:\n\n|\n\}\s*\Z)", dump, re.S)
    if not m:
        die(f"{signature} not found - this Niagara differs from the one this was written against")
    return m.group(1)


def push(op):
    """The int a constant-push instruction pushes."""
    if op.startswith("iconst_"):
        return int(op.split("_")[1])
    if op.split()[0] in ("bipush", "sipush"):
        return int(op.split()[1])
    m = re.search(r"// int (-?\d+)", op)
    if not m:
        die(f"cannot read a constant out of {op!r}")
    return int(m.group(1))


# --------------------------------------------------------------------------
# Everything below is read out of the install. Nothing here is a table typed
# out from the EDE spec, because the spec is not what imports the file.
# --------------------------------------------------------------------------
def read_rules(home):
    ede = unpack(home / "modules" / "bacnetEDE-wb.jar", "ede-")
    bac = unpack(home / "modules" / "bacnet-rt.jar", "bacnet-")

    cur = disasm(ede, f"{EDE}.EdeCursor")

    # Column order: the recordProps array, built once in the static initializer.
    static = re.search(r"static \{\};\n(.*?)(?:\n\s+\d+: return|\Z)", cur, re.S)
    if not static:
        die("EdeCursor has no static initializer")
    cols = re.findall(r"// Field com/tridium/bacnetEde/BEdeRecord\.(\w+):Ljavax/baja/sys/Property;",
                      static.group(1))
    size = re.search(r"\d+: ([^\n]+)\n\s+\d+: anewarray\s+#\d+\s+// class javax/baja/sys/Property",
                     static.group(1))
    if not cols or not size or push(size.group(1)) != len(cols):
        die(f"recordProps did not parse: {len(cols)} names for a {size and size.group(1)} array")

    # The required rule. Read the constant; do not assume it is five.
    pr = method(cur, "private void parseRecord(java.lang.String[])")
    req = re.search(r"iload_2\n\s+\d+: (iconst_\d|bipush\s+\d+)\n\s+\d+: if_icmpge", pr)
    if not req:
        die("parseRecord no longer compares the column index against a constant")
    required_below = push(req.group(1))

    # Header rows and the comment marker, from skipLine.
    skip = method(cur, "private boolean skipLine(java.lang.String[])")
    marks = re.findall(r'ldc\s+#\d+\s+//\s+String (\S+)\n\s+\d+: invokevirtual.*String\.startsWith', skip)
    if "#" not in marks or len(marks) < 2:
        die("skipLine no longer matches a comment marker plus header rows")
    comment = "#"
    headers = [m for m in marks if m != "#"]

    # Object type groups, from EDEUtil's switches.
    util = disasm(ede, f"{EDE}.util.EDEUtil")
    groups = {}
    for name in ("isPoint", "isBooleanPoint", "isMultiStatePoint", "isNumericPoint"):
        body = method(util, f"public static boolean {name}(int)")
        cases = [(int(a), int(b)) for a, b in re.findall(r"^\s+(\d+): (\d+)$", body, re.M)]
        true_pc = [int(m.group(1)) for m in re.finditer(r"(\d+): iconst_1", body)]
        if not cases or len(true_pc) != 1:
            die(f"{name} is no longer a switch with one true branch")
        groups[name] = {n for n, t in cases if t == true_pc[0]}

    # Object type names, from the bacnet module's own table.
    xml = (bac / "com/tridium/bacnet/objectTypes.xml").read_text("utf8", errors="replace")
    types = {int(m.group(2)): m.group(1)
             for m in re.finditer(r'<object n="([^"]+)" t="(\d+)"', xml)}
    if 8 not in types:
        die("objectTypes.xml did not parse - refusing to print type numbers with guessed names")

    # Unit codes that resolve. getNiagaraUnits(int) returns null for anything
    # outside the frozen enum, and parseRecordVal turns that null into a thrown
    # invalidValue, so this set is the difference between a line and a failure.
    eu = disasm(bac, "javax.baja.bacnet.enums.BBacnetEngineeringUnits")
    estatic = re.search(r"static \{\};\n(.*)", eu, re.S)
    if not estatic:
        die("BBacnetEngineeringUnits has no static initializer")
    units = {}
    for m in re.finditer(
            r"// class javax/baja/bacnet/enums/BBacnetEngineeringUnits\n"
            r"\s+\d+: dup\n\s+\d+: ([^\n]+)\n"
            r"\s+\d+: (?:ldc|ldc_w)\s+#\d+\s+// String (.*)\n"
            r"\s+\d+: invokespecial", estatic.group(1)):
        units[push(m.group(1))] = m.group(2)
    if len(units) < 200:
        die(f"only {len(units)} unit codes parsed - read BBacnetEngineeringUnits by hand")

    # The configured delimiter default, which is not the one EDE files use.
    cfg = disasm(ede, f"{EDE}.BEdeConfig")
    dm = re.search(r"ldc\s+#\d+\s+// String (\S)\n\s+\d+: \S+\n\s+\d+: invokestatic.*newProperty"
                   r".*\n\s+\d+: putstatic\s+#\d+\s+// Field delimiter", cfg)
    config_delimiter = dm.group(1) if dm else None

    return dict(home=home, cols=cols, required_below=required_below, comment=comment,
                headers=headers, groups=groups, types=types, units=units,
                config_delimiter=config_delimiter)


# --------------------------------------------------------------------------
# Writing a file the parser will accept.
# --------------------------------------------------------------------------
# These are the EDE spec's own column titles, in the jar's column order. They
# are only ever written into a commented-out line, so a wrong one cannot change
# what imports; the import is positional.
TITLES = {
    "keyName": "keyname",
    "deviceObjInstant": "device obj.-instance",
    "objectName": "object-name",
    "objectType": "object-type",
    "objInstance": "object-instance",
    "description": "description",
    "presentValueDefault": "present-value-default",
    "minPresentValue": "min-present-value",
    "maxPresentValue": "max-present-value",
    "settable": "settable",
    "cov": "supports COV",
    "hiLimit": "hi-limit",
    "lowLimit": "low-limit",
    "stateTextRange": "state-text-reference",
    "units": "unit-code",
    "vendorSpecificAddress": "vendor-specific-address",
    "notificationClass": "notification-class",
}


def write_template(rules, path, states_path, delim):
    cols = rules["cols"]
    n = len(cols)
    req = rules["required_below"]
    rows = [
        ["PROJECT_NAME", "a name for this reference file"],
        ["AUTHOR_OF_LAST_CHANGE", ""],
        ["VERSION_OF_REFERENCEFILE", "1"],
        ["TIMESTAMP_OF_LAST_CHANGE", ""],
        ["LIMITED_RESOURCES", ""],
        ["VERSION_OF_LAYOUT", "2"],
        [f"{rules['comment']} the next line is a comment too - the import is positional, "
         f"column {req + 1} onward is optional, and a blank one is silently defaulted"],
        [f"{rules['comment']} " + TITLES.get(cols[0], cols[0])] +
        [TITLES.get(c, c) for c in cols[1:]],
    ]
    # One example per group, so each type rule is exercised by the file itself.
    examples = [
        ("AHU1_SAT", 1001, "AHU1_SupplyAirTemp", 0, 1, "supply air temperature",
         "21.5", "-40", "120", "n", "y", "", "", "", "64", "", ""),
        ("AHU1_FAN_CMD", 1001, "AHU1_SupplyFanCmd", 4, 1, "supply fan command",
         "false", "", "", "y", "y", "", "", "binary_active_inactive", "95", "", ""),
        # On a multi-state type the present-value default is looked up in the
        # state-text range by name, not by ordinal, so "1" would discard it.
        ("AHU1_MODE", 1001, "AHU1_OccupancyMode", 19, 1, "occupancy mode",
         "Occupied", "", "", "y", "n", "", "", "occupancy_modes", "95", "", ""),
    ]
    for ex in examples:
        if len(ex) != n:
            die(f"the example rows have {len(ex)} fields and the jar wants {n}")
        rows.append([str(v) for v in ex])
    path.write_text("".join(delim.join(r) + "\r\n" for r in rows), encoding="utf8")
    print(f"wrote {path} - {n} columns, first {req} mandatory, delimiter {delim!r}")

    if states_path:
        # name, then one text per state, ordinal 1..n in the order written.
        st = [
            [f"{rules['comment']} name, then one text per state - ordinals are assigned 1..n "
             f"in this order, and a blank one is dropped, which shifts every later ordinal"],
            ["binary_active_inactive", "Inactive", "Active"],
            ["occupancy_modes", "Occupied", "Unoccupied", "Standby"],
        ]
        states_path.write_text("".join(delim.join(r) + "\r\n" for r in st), encoding="utf8")
        print(f"wrote {states_path} - the state-text lookup the stateTextRange column names")


# --------------------------------------------------------------------------
# Checking a file the way the parser would read it.
# --------------------------------------------------------------------------
def split_line(line, delim):
    """CSVReader's parseLine: a quoted field may hold the delimiter."""
    out, buf, quoted = [], [], False
    for ch in line:
        if ch == '"':
            quoted = not quoted
        elif ch == delim and not quoted:
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    out.append("".join(buf))
    return out


def detect_delimiter(text, rules):
    body = [l for l in text.splitlines() if l.strip()
            and not l.strip().startswith(rules["comment"])
            and not any(l.strip().upper().startswith(h) for h in rules["headers"])]
    if not body:
        return None
    counts = {d: sum(l.count(d) for l in body) for d in ";,\t"}
    best = max(counts, key=counts.get)
    return best if counts[best] else None


def check(rules, path, states_path, delim):
    cols = rules["cols"]
    req = rules["required_below"]
    groups = rules["groups"]
    text = path.read_text(encoding="utf8", errors="replace")

    if delim is None:
        delim = detect_delimiter(text, rules)
        if delim is None:
            die(f"{path} has no data lines, or no delimiter that splits them")
        print(f"delimiter: {delim!r} (detected)")
    else:
        print(f"delimiter: {delim!r} (given)")
    if rules["config_delimiter"] and delim != rules["config_delimiter"]:
        print(f"  note: BEdeConfig's delimiter property defaults to "
              f"{rules['config_delimiter']!r}. Set it to {delim!r} on the EdeConfig "
              f"before importing this file, or every line arrives as one field.")

    states = {}
    if states_path:
        for raw in states_path.read_text(encoding="utf8", errors="replace").splitlines():
            f = split_line(raw, delim)
            key = f[0].strip()
            if not key or key.startswith(rules["comment"]):
                continue
            states[key] = [v for v in f[1:] if v != ""]

    errors, warnings, points, devices, skipped, trendlogs = [], [], 0, 0, 0, 0
    seen_headers = set()
    # A blank optional column is the whole point of this check, but on a real
    # file most of them are blank on every line. Per-line only for the two that
    # cost something operationally; the rest are counted and reported once.
    loud_blanks = {"units", "presentValueDefault"}
    blank_counts = {}

    for ln, raw in enumerate(text.splitlines(), 1):
        if not raw.strip():
            continue
        f = split_line(raw, delim)
        first = f[0].upper()
        if first.startswith(rules["comment"]):
            skipped += 1
            continue
        hit = next((h for h in rules["headers"] if first.startswith(h)), None)
        if hit:
            seen_headers.add(hit)
            skipped += 1
            continue

        def err(msg):
            errors.append(f"line {ln}: {msg}")

        def warn(msg):
            warnings.append(f"line {ln}: {msg}")

        if len(f) < req:
            err(f"only {len(f)} fields; the first {req} are mandatory")
            continue

        vals = dict(zip(cols, f))
        # A device row and a trend-log row never become points, so the blanks
        # that cost a point its units or its default cost them nothing.
        try:
            rowtype = int(vals.get("objectType", "").strip())
        except ValueError:
            rowtype = None
        point_row = rowtype not in (8, 20)
        for i, name in enumerate(cols[:len(f)]):
            if f[i].strip() == "":
                if i < req:
                    err(f"{name} (column {i + 1}) is empty and mandatory - "
                        f"parseRecordVal throws valueMissing and the line is dropped")
                else:
                    blank_counts[name] = blank_counts.get(name, 0) + 1
                    if name in loud_blanks and point_row:
                        warn(f"{name} (column {i + 1}) is empty - silently replaced by its "
                             f"property default, so this point arrives "
                             f"{'unitless' if name == 'units' else 'with no default'}")

        otype = None
        if vals.get("objectType", "").strip():
            try:
                otype = int(vals["objectType"].strip())
            except ValueError:
                err(f"objectType {vals['objectType']!r} is not an integer - "
                    f"BInteger.make throws and the line is dropped")
            else:
                if otype not in rules["types"]:
                    err(f"objectType {otype} is not a type in this Niagara's table")
                elif otype == 8:
                    devices += 1
                elif otype == 20:
                    # The one non-point type the module still wants:
                    # BHistoryImportDiscoveryJob walks this same file, keeps the
                    # rows whose objectType is trendLog and whose device
                    # instance matches, and offers each one as a history.
                    trendlogs += 1
                elif otype not in groups["isPoint"]:
                    warn(f"objectType {otype} ({rules['types'][otype]}) is not in EDEUtil.isPoint, "
                         f"so the row parses but no point is offered")
                else:
                    points += 1

        # parseRecord walks min(fields, columns). Extra fields are ignored;
        # missing trailing columns are never even visited, so they default
        # without a word - which is why a five-column file imports clean. A
        # device row is short by design, so it is not worth a line here.
        if len(f) > len(cols):
            warn(f"{len(f)} fields, {len(cols)} columns - the extra {len(f) - len(cols)} are ignored")
        if len(f) < len(cols) and otype != 8:
            warn(f"{len(f)} fields - columns {len(f) + 1}-{len(cols)} "
                 f"({', '.join(cols[len(f):])}) are never read and take their property defaults")

        for name, cast, how in (("deviceObjInstant", int, "BInteger.make"),
                                ("objInstance", int, "BInteger.make"),
                                ("notificationClass", int, "BInteger.make"),
                                ("minPresentValue", float, "BDouble.make"),
                                ("maxPresentValue", float, "BDouble.make"),
                                ("hiLimit", float, "BDouble.make"),
                                ("lowLimit", float, "BDouble.make")):
            v = vals.get(name, "").strip()
            if v:
                try:
                    cast(v)
                except ValueError:
                    err(f"{name} {v!r} does not survive {how} - invalidValue, line dropped")

        pv = vals.get("presentValueDefault", "")
        if pv and otype is not None:
            if otype in groups["isNumericPoint"]:
                try:
                    float(pv)
                except ValueError:
                    err(f"presentValueDefault {pv!r} on a numeric type - "
                        f"BDouble.make throws, line dropped")
            elif otype in groups["isBooleanPoint"] and pv.strip().lower() not in ("true", "false"):
                warn(f"presentValueDefault {pv!r} on a boolean type reads as false; "
                     f"the test is equality with \"true\" (any case), so \"1\", \"active\" "
                     f"and \"on\" all mean false")

        for name in ("settable", "cov"):
            v = vals.get(name, "").strip()
            if v and v.lower() not in ("y", "n"):
                warn(f"{name} {v!r} reads as false - the test is equality with \"y\", "
                     f"so \"yes\", \"1\" and \"TRUE\" all mean no")

        u = vals.get("units", "").strip()
        if u:
            try:
                code = int(u)
            except ValueError:
                err(f"units {u!r} is not an integer - Integer.parseInt throws, line dropped")
            else:
                if code not in rules["units"]:
                    err(f"unit code {code} is not one of this Niagara's "
                        f"{len(rules['units'])} codes - getNiagaraUnits returns null and the "
                        f"line is dropped")

        st = vals.get("stateTextRange", "").strip()
        if st:
            if not states_path:
                warn(f"stateTextRange {st!r} but no state-text file given to check it against; "
                     f"if the EdeConfig has none either, the line is dropped")
            elif st not in states:
                err(f"stateTextRange {st!r} is not in {states_path.name} - the lookup misses, "
                    f"the range is NULL and a non-empty reference throws invalidValue")
            elif otype in groups["isBooleanPoint"] and len(states[st]) != 2:
                warn(f"stateTextRange {st!r} has {len(states[st])} texts on a boolean type; "
                     f"anything but two is discarded and the point arrives unenumerated")
            elif otype in groups["isMultiStatePoint"] and pv and pv not in states[st]:
                warn(f"presentValueDefault {pv!r} is not one of the texts in {st!r}. On a "
                     f"multi-state type the default is looked up in the range by name, not "
                     f"by ordinal, and a miss discards the whole range - the point arrives "
                     f"unenumerated. Texts here: {', '.join(states[st])}")

    missing = [h for h in rules["headers"] if h not in seen_headers]
    print(f"\n{path}: {points} point rows, {devices} device rows, "
          f"{trendlogs} trend-log row{'' if trendlogs == 1 else 's'}, "
          f"{skipped} header or comment lines")
    if trendlogs:
        one = trendlogs == 1
        print(f"  note: {'that trendLog row is' if one else f'those {trendlogs} trendLog rows are'}"
              f" invisible to point discovery (isPoint(20)")
        print(f"        is false), but BHistoryImportDiscoveryJob reads the same file and")
        print(f"        offers {'it' if one else 'each one'} as a history, typed 'Unknown'.")
    if missing:
        print(f"  header rows absent: {', '.join(missing)} "
              f"(harmless to the parser, which skips by prefix, but EDE readers expect them)")
    for e in errors:
        print(f"  ERROR  {e}")
    for w in warnings:
        print(f"  warn   {w}")
    quiet = {n: c for n, c in blank_counts.items() if n not in loud_blanks}
    if quiet:
        print("  blank optional columns, each silently taking its property default: "
              + ", ".join(f"{n} x{c}" for n, c in sorted(quiet.items(), key=lambda kv: -kv[1])))
    if not errors and not warnings and not quiet:
        print("  nothing to report: every column parses and none is silently defaulted")
    print(f"\nRead from {rules['home'].name}: {len(cols)} columns, first {req} mandatory, "
          f"{len(rules['units'])} unit codes, {len(groups['isPoint'])} object types that "
          f"become points.")
    return 1 if errors else 0


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--template", type=Path)
    ap.add_argument("--check", type=Path)
    ap.add_argument("--states", type=Path)
    ap.add_argument("--delimiter")
    ap.add_argument("home", nargs="?", default=os.environ.get(
        "NIAGARA_HOME", "/opt/Niagara/Niagara-4.15.5.22"))
    a = ap.parse_args()
    if bool(a.template) == bool(a.check):
        ap.error("give exactly one of --template or --check")
    if a.delimiter is not None and len(a.delimiter) != 1:
        ap.error("--delimiter takes one character")
    global JAVAP
    JAVAP = _find_javap(a.home)
    if not JAVAP:
        die("no javap found - set $JAVAP or put a JDK 8 javap on PATH")

    rules = read_rules(Path(a.home))
    if a.template:
        write_template(rules, a.template, a.states, a.delimiter or ";")
        return 0
    return check(rules, a.check, a.states, a.delimiter)


if __name__ == "__main__":
    sys.exit(main())
