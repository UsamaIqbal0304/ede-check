# ede-check

An EDE file checked against the parser Niagara actually ships, rather than
against the EDE spec.

One Python file, standard library only. It reads the import rules out of
`bacnetEDE-wb.jar` in a Niagara installation with `javap`, then reports, line
by line, what the parser would reject and what it would silently default.

```
./ede-check.py --template points.csv [--states states.csv]
./ede-check.py --check points.csv [--states states.csv] [--delimiter ';']
(optional last argument, or $NIAGARA_HOME: the Niagara install to read)
```

## Why this exists

An EDE file that imports without an error is not the same as an EDE file that
imports correctly. The parser demands five columns and quietly substitutes a
property default for every later column that is blank, so a file with no unit
codes and no state texts imports clean and lands as unitless points with no
enumeration. Nothing in the job log says so.

The one that costs an afternoon is simpler still: `BEdeConfig`'s delimiter
property defaults to a comma while EDE files are semicolon delimited, so until
someone changes it on the EdeConfig, every line arrives as a single field.

## What it reads, and where from

Nothing here is quoted from documentation. Each rule is read out of the
bytecode of the installed jar:

- `EdeCursor`'s `recordProps` array gives the column order, positionally.
- `parseRecord` passes `required=true` only while the column index is below a
  constant this reads off the bytecode rather than assuming it is five.
- `skipLine` gives the header rows that are skipped, by prefix on the
  uppercased first field, plus the comment marker.
- `parseRecordVal` gives the per-column type rules - which columns go through
  `BInteger.make`, which through `BDouble.make`, which are "y" or not-"y", and
  which are escaped free text.
- `BBacnetEngineeringUnits` gives the unit codes that resolve to a Niagara
  unit. A code outside that set fails the line, and the set is not contiguous.
- `EDEUtil` gives the object types that become points at all, and the numeric,
  multi-state and boolean groups that decide how `presentValueDefault` is read.

Because the rules come from the jar, the output cannot drift from the Niagara
version you are actually running.

## Read first: what it does and does not touch

**It never connects to a station, a device or a network.** It unzips jars and
runs `javap`. Nothing is installed, patched, written to a station or sent
anywhere. The only file it writes is the one you pass to `--template`.

It needs two things on the machine: a Niagara installation to read, and a
`javap` from a JDK 8. It looks for `javap` in `$JAVAP`, then on `PATH`, then
under `$JAVA_HOME` and the Niagara install, then in Debian's default location.

**The numbers below were measured against Niagara 4.15.5.22.** Another version
may differ, and that is the point - rerun it against yours rather than
trusting this page.

## Running it

`--template` writes a file whose columns are in the jar's order, with the
header rows the jar skips:

```
$ ./ede-check.py --template points.csv
wrote points.csv - 17 columns, first 5 mandatory, delimiter ';'
```

`--check` reports what would happen on import. `examples/ede-sample.csv` in
this repo is that template with five rows, four of them deliberately wrong:

```
$ ./ede-check.py --check examples/ede-sample.csv
delimiter: ';' (detected)
  note: BEdeConfig's delimiter property defaults to ','. Set it to ';' on the EdeConfig before importing this file, or every line arrives as one field.

examples/ede-sample.csv: 4 point rows, 0 device rows, 0 trend-log rows, 8 header or comment lines
  ERROR  line 11: unit code 999 is not one of this Niagara's 269 codes - getNiagaraUnits returns null and the line is dropped
  warn   line 10: units (column 15) is empty - silently replaced by its property default, so this point arrives unitless
  warn   line 13: objectType 10 (File) is not in EDEUtil.isPoint, so the row parses but no point is offered
  blank optional columns, each silently taking its property default: hiLimit x5, lowLimit x5, stateTextRange x5, vendorSpecificAddress x5, notificationClass x5, minPresentValue x1, maxPresentValue x1

Read from Niagara-4.15.5.22: 17 columns, first 5 mandatory, 269 unit codes, 13 object types that become points.
```

Exit status is 1 when any line would be rejected, 0 when the file is clean.

## The same finding, written up

The output above, the 17 columns, which 5 are mandatory, and what each blank optional column becomes once Niagara has imported it, is also a page: <https://plantroomlabs.com/tools/ede-check/>. It carries this run, the download with its size and SHA-256, and the note explaining the reasoning.

## Licence

MIT. Written by Usama Iqbal at [Plantroom Labs](https://plantroomlabs.com).
