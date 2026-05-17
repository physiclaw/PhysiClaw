# PhysiClaw — CoreXY Frame + Slider Subassembly BOM

> **Rev:** 0.3 · **Date:** 2026-05-17 · **BOM type:** eBOM (design stage)
> **Assembly P/N:** PC-ASM-CXY-01 · **Currency:** CNY · **Maintainer:** QIAOQIAN

Item numbers match the balloon callouts on drawing `PC-DWG-CXY-01`.
`Source = Make` parts have no unit price — build123d source lives in
`hardware/parts/custom/<name>.py`; STEPs are generated to
`hardware/output/step/<name>_x<qty>.step` via `python -m hardware`.

---

## 1. Frame

|   # | Part No.        | Description             | Spec              | Qty | Unit | Source | Material   | Supplier P/N | Unit Price | Subtotal | Remarks       |
| --: | --------------- | ----------------------- | ----------------- | --: | ---- | ------ | ---------- | ------------ | ---------: | -------: | ------------- |
|   1 | PC-FRM-2020-400 | Aluminum extrusion 2020 | L 400 mm          |   4 | pc   | Buy    | 6063-T5    | EU2020-400   |      15.00 |    60.00 | Cut to length |
|   2 | PC-FRM-COR-2020 | Corner bracket 2020     | Inner hidden type |   8 | pc   | Buy    | Zinc alloy | CB2020-I     |       2.00 |    16.00 |               |
|   3 | PC-FRM-FOOT     | Rubber foot             | M6, D20           |   4 | pc   | Buy    | Rubber     | FT-M6-20     |       1.00 |     4.00 |               |

Section subtotal: **¥ 80.00**

## 2. Motion

|   # | Part No.         | Description           | Spec               | Qty | Unit | Source | Material | Supplier P/N | Unit Price | Subtotal | Remarks              |
| --: | ---------------- | --------------------- | ------------------ | --: | ---- | ------ | -------- | ------------ | ---------: | -------: | -------------------- |
|   4 | PC-MOT-MGN12-350 | Linear rail MGN12H    | L 350 mm, w/ block |   2 | set  | Buy    | —        | MGN12H-350   |      45.00 |    90.00 | Y axis, A/B side     |
|   5 | PC-MOT-MGN12-300 | Linear rail MGN12H    | L 300 mm, w/ block |   1 | set  | Buy    | —        | MGN12H-300   |      40.00 |    40.00 | X axis (gantry)      |
|   6 | PC-SLD-YCAR-01   | Y-carriage plate      | Custom             |   2 | pc   | Make   | Al 6061  | —            |          — |        — | build123d source TBD |
|   7 | PC-SLD-XCAR-01   | X-carriage (toolhead) | Custom             |   1 | pc   | Make   | Al 6061  | —            |          — |        — | build123d source TBD |

Section subtotal: **¥ 130.00**

## 3. Transmission

|   # | Part No.       | Description           | Spec              | Qty | Unit   | Source | Material     | Supplier P/N | Unit Price | Subtotal | Remarks                               |
| --: | -------------- | --------------------- | ----------------- | --: | ------ | ------ | ------------ | ------------ | ---------: | -------: | ------------------------------------- |
|   8 | PC-TRN-GT2-16  | Timing belt GT2-6 mm  | Open-end, ~1.6 m  |   2 | length | Buy    | Rubber/fiber | GT2-6-OE     |       8.00 |    16.00 | Belt A / Belt B                       |
|   9 | PC-TRN-PUL-20T | Timing pulley 20T     | GT2, 5 mm bore    |   2 | pc     | Buy    | Aluminum     | GT2-20T-5    |       6.00 |    12.00 | Motor shaft                           |
|  10 | PC-TRN-IDL-BIG | Idler pulley, toothed | 5 mm bore, w/ brg |   2 | pc     | Buy    | —            | IDL-GT2-5    |       4.00 |     8.00 | Corner                                |
|  11 | PC-TRN-IDL-SML | Idler pulley, smooth  | 5 mm bore, w/ brg |   4 | pc     | Buy    | —            | IDL-SM-5     |       3.50 |    14.00 | Turn points                           |
|  12 | PC-TRN-BCLP-01 | Belt clamp            | Custom            |   2 | pc     | Make   | PETG         | —            |          — |        — | `hardware/parts/custom/belt_clamp.py` |

Section subtotal: **¥ 50.00**

## 4. Drive

|   # | Part No.      | Description          | Spec           | Qty | Unit | Source | Material | Supplier P/N | Unit Price | Subtotal | Remarks           |
| --: | ------------- | -------------------- | -------------- | --: | ---- | ------ | -------- | ------------ | ---------: | -------: | ----------------- |
|  13 | PC-DRV-NEMA17 | Stepper motor NEMA17 | 42-40, 1.5 A   |   2 | pc   | Buy    | —        | 17HS4401     |      42.00 |    84.00 | Motor A / Motor B |
|  14 | PC-DRV-MNT-17 | Motor mount bracket  | NEMA17, L-type |   2 | pc   | Buy    | Aluminum | MM-17-L      |       6.00 |    12.00 |                   |

Section subtotal: **¥ 96.00**

## 5. Standard Parts (GB) — roll-up

Listed once per GB code, grouped by family (screws → nuts → washers/rings).

|   # | Standard Code | Description           | Spec     | Qty | Unit | Remarks             |
| --: | ------------- | --------------------- | -------- | --: | ---- | ------------------- |
|  15 | GB/T 70.1     | Socket head cap screw | M3×8     |  30 | pc   | Carriage / brackets |
|  16 | GB/T 70.1     | Socket head cap screw | M5×16    |  16 | pc   | Frame / rail        |
|  17 | GB/T 70.1     | Socket head cap screw | M5×10    |  12 | pc   | Rail to extrusion   |
|  18 | GB/T 6170     | Hex nut               | M5       |  16 | pc   |                     |
|  19 | GB/T 818      | T-slot nut            | 2020, M5 |  28 | pc   | Extrusion fastening |
|  20 | GB/T 894      | Retaining ring        | Shaft 5  |   8 | pc   | Idler shafts        |

---

## Summary

**Self-fabricated parts** (`Source = Make`):

| #   | Part                  | Source / Status                       |
| --- | --------------------- | ------------------------------------- |
| 6   | Y-carriage plate      | build123d source TBD                  |
| 7   | X-carriage (toolhead) | build123d source TBD                  |
| 12  | Belt clamp            | `hardware/parts/custom/belt_clamp.py` |

Generated STEPs land in `hardware/output/step/` after `python -m hardware`.

**Purchased subtotal (excl. standard parts):**

| Section         |          ¥ |
| --------------- | ---------: |
| 1. Frame        |      80.00 |
| 2. Motion       |     130.00 |
| 3. Transmission |      50.00 |
| 4. Drive        |      96.00 |
| **Subtotal**    | **356.00** |

**Standard parts:** estimate per fastener kit, fill after sourcing.
**Total:** **¥ TBD**

---

### Field legend

| Field                     | Meaning                                                                  |
| ------------------------- | ------------------------------------------------------------------------ |
| `#`                       | Item number — matches drawing balloon callout                            |
| `Part No.`                | Unique part code (mandatory). Make = own drawing no.; standard = GB code |
| `Spec`                    | Size / variant detail                                                    |
| `Qty` / `Unit`            | Quantity and unit of issue (pc / set / length)                           |
| `Source`                  | `Make` (self-fabricated, has STEP) or `Buy` (purchased)                  |
| `Material`                | Required for Make parts; drives process & cost                           |
| `Supplier P/N`            | Vendor part number for reorder / reproducibility                         |
| `Unit Price` / `Subtotal` | `Subtotal = Qty × Unit Price`                                            |
| `Remarks`                 | Cut size, surface finish, A/B-side notes                                 |

> Revision is kept in the file header (one source of truth), not per row.
> When the assembly is split into sub-assemblies, add a `Level` column
> (`.1`, `..2`, …) to migrate this flat list to a multi-level BOM.
