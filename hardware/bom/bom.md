# PhysiClaw — Full Machine BOM

> **Rev:** 0.8 · **Date:** 2026-05-17 · **BOM type:** eBOM (design stage)
> **Assembly P/N:** PC-ASM-TOP-01 · **Currency:** CNY · **Maintainer:** QIAOQIAN

Top-level machine BOM. The CoreXY frame + slider is subassembly
**PC-ASM-CXY-01** (sections 1–5); electronics & power is subassembly
**PC-ASM-ELP-01** (section 6).

Item numbers match the balloon callouts on drawing `PC-DWG-CXY-01` (mechanical)
and wiring diagram `PC-DWG-ELP-01` (electrical).
`Process = Print` or `Machine` parts have no unit price — build123d source
lives in `hardware/parts/custom/<name>.py`; STEPs are generated to
`hardware/output/step/<name>_x<qty>.step` via `python -m hardware`.

> `Vendor` / `Link` cells are real only for frame extrusions; the rest
> show `TBD` — deliberately not guessed (see legend).

---

## Subassembly PC-ASM-CXY-01 — CoreXY Frame + Slider

### 1. Frame

> Connection method: **bolt end-fastening** — no corner brackets.
> The 2020-170 bars are tapped M6 on both ends; the 2020-335 bars have a
> Ø6.6 through-hole with a Ø11 counterbore 10 mm from each end, so the
> M6×25 socket-head cap screws pass through and thread into the tapped
> ends. Profile cutting and machining are done by the supplier — these are
> **Outsourced** (buy-to-print), not off-the-shelf.
> Extrusion unit prices include cut-waste allowance.

|   # | Part No.        | Description                 | Spec                     | Qty | Unit | Process    | Material            | Supplier P/N | Vendor                   | Link | Unit Price | Subtotal | Remarks                                               |
| --: | --------------- | --------------------------- | ------------------------ | --: | ---- | ---------- | ------------------- | ------------ | ------------------------ | ---- | ---------: | -------: | ----------------------------------------------------- |
|   1 | PC-FRM-2020-170 | Aluminum extrusion 2020     | L 170 mm, black anodized |   2 | pc   | Outsourced | 6063-T5             | TDT-2020-BLK | Jiangsu Ruizhou (Taobao) | TBD  |       4.93 |     9.86 | Both ends tapped M6                                   |
|   2 | PC-FRM-2020-335 | Aluminum extrusion 2020     | L 335 mm, black anodized |   2 | pc   | Outsourced | 6063-T5             | TDT-2020-BLK | Jiangsu Ruizhou (Taobao) | TBD  |       9.71 |    19.42 | Counterbore Ø6.6 through, Ø11 c'bore, 10 mm from ends |
|   3 | PC-FRM-1020-165 | Aluminum extrusion 1020     | L 165 mm, black anodized |   1 | pc   | Outsourced | 6063-T5             | TDT-1020-BLK | Jiangsu Ruizhou (Taobao) | TBD  |       1.72 |     1.72 | New profile (10×20 cross-section)                     |
|   4 | PC-FRM-FAST-M6  | Hex socket cap bolt, nickel | M6×25                    |   5 | pc   | Standard   | Nickel-plated steel | TDT-M6-25    | Jiangsu Ruizhou (Taobao) | TBD  |       0.20 |     1.00 | End-fastening bolt (x-ref §5 item 20)                 |
|   5 | PC-FRM-FOOT     | Rubber foot                 | M6, D20                  |   4 | pc   | COTS       | Rubber              | FT-M6-20     | TBD                      | TBD  |       1.00 |     4.00 |                                                       |

Section subtotal: **¥ 48.00** (parts ¥36.00 + supplier machining ¥12.00)
Supplier quote all-in (incl. freight + tax, discounted): **¥ 43.00**

### 2. Motion

|   # | Part No.         | Description           | Spec               | Qty | Unit | Process | Material | Supplier P/N | Vendor | Link | Unit Price | Subtotal | Remarks          |
| --: | ---------------- | --------------------- | ------------------ | --: | ---- | ------- | -------- | ------------ | ------ | ---- | ---------: | -------: | ---------------- |
|   6 | PC-MOT-MGN12-350 | Linear rail MGN12H    | L 350 mm, w/ block |   2 | set  | COTS    | —        | MGN12H-350   | TBD    | TBD  |      45.00 |    90.00 | Y axis, A/B side |
|   7 | PC-MOT-MGN12-300 | Linear rail MGN12H    | L 300 mm, w/ block |   1 | set  | COTS    | —        | MGN12H-300   | TBD    | TBD  |      40.00 |    40.00 | X axis (gantry)  |
|   8 | PC-SLD-YCAR-01   | Y-carriage plate      | Custom             |   2 | pc   | Machine | Al 6061  | —            | Self   | —    |          — |        — |                  |
|   9 | PC-SLD-XCAR-01   | X-carriage (toolhead) | Custom             |   1 | pc   | Machine | Al 6061  | —            | Self   | —    |          — |        — |                  |

Section subtotal: **¥ 130.00**

### 3. Transmission

|   # | Part No.       | Description           | Spec              | Qty | Unit   | Process | Material     | Supplier P/N | Vendor | Link | Unit Price | Subtotal | Remarks                               |
| --: | -------------- | --------------------- | ----------------- | --: | ------ | ------- | ------------ | ------------ | ------ | ---- | ---------: | -------: | ------------------------------------- |
|  10 | PC-TRN-GT2-16  | Timing belt GT2-6 mm  | Open-end, ~1.6 m  |   2 | length | COTS    | Rubber/fiber | GT2-6-OE     | TBD    | TBD  |       8.00 |    16.00 | Belt A / Belt B                       |
|  11 | PC-TRN-PUL-20T | Timing pulley 20T     | GT2, 5 mm bore    |   2 | pc     | COTS    | Aluminum     | GT2-20T-5    | TBD    | TBD  |       6.00 |    12.00 | Motor shaft                           |
|  12 | PC-TRN-IDL-BIG | Idler pulley, toothed | 5 mm bore, w/ brg |   2 | pc     | COTS    | —            | IDL-GT2-5    | TBD    | TBD  |       4.00 |     8.00 | Corner                                |
|  13 | PC-TRN-IDL-SML | Idler pulley, smooth  | 5 mm bore, w/ brg |   4 | pc     | COTS    | —            | IDL-SM-5     | TBD    | TBD  |       3.50 |    14.00 | Turn points                           |
|  14 | PC-TRN-BCLP-01 | Belt clamp            | Custom            |   2 | pc     | Print   | PETG         | —            | Self   | —    |          — |        — | `hardware/parts/custom/belt_clamp.py` |

Section subtotal: **¥ 50.00**

### 4. Drive

|   # | Part No.      | Description          | Spec           | Qty | Unit | Process | Material | Supplier P/N | Vendor | Link | Unit Price | Subtotal | Remarks           |
| --: | ------------- | -------------------- | -------------- | --: | ---- | ------- | -------- | ------------ | ------ | ---- | ---------: | -------: | ----------------- |
|  15 | PC-DRV-NEMA17 | Stepper motor NEMA17 | 42-40, 1.5 A   |   2 | pc   | COTS    | —        | 17HS4401     | TBD    | TBD  |      42.00 |    84.00 | Motor A / Motor B |
|  16 | PC-DRV-MNT-17 | Motor mount bracket  | NEMA17, L-type |   2 | pc   | COTS    | Aluminum | MM-17-L      | TBD    | TBD  |       6.00 |    12.00 |                   |

Section subtotal: **¥ 96.00**

### 5. Standard Parts (GB) — roll-up

Listed once per GB code, grouped by family (screws → nuts → washers/rings).

|   # | Standard Code | Description           | Spec     | Qty | Unit | Vendor | Link | Remarks                                      |
| --: | ------------- | --------------------- | -------- | --: | ---- | ------ | ---- | -------------------------------------------- |
|  17 | GB/T 70.1     | Socket head cap screw | M3×8     |  30 | pc   | TBD    | TBD  | Carriage / brackets                          |
|  18 | GB/T 70.1     | Socket head cap screw | M5×16    |  16 | pc   | TBD    | TBD  | Frame / rail                                 |
|  19 | GB/T 70.1     | Socket head cap screw | M5×10    |  12 | pc   | TBD    | TBD  | Rail to extrusion                            |
|  20 | GB/T 70.1     | Socket head cap screw | M6×25    |   5 | pc   | —      | —    | x-ref → §1 item 4 (cost & vendor live there) |
|  21 | GB/T 6170     | Hex nut               | M5       |  16 | pc   | TBD    | TBD  |                                              |
|  22 | GB/T 818      | T-slot nut            | 2020, M5 |  28 | pc   | TBD    | TBD  | Extrusion fastening                          |
|  23 | GB/T 894      | Retaining ring        | Shaft 5  |   8 | pc   | TBD    | TBD  | Idler shafts                                 |

> Item 20 (M6×25) is the same physical bolt as §1 item 4 (PC-FRM-FAST-M6),
> procured together with the extrusions. Listed here as a standard-part
> cross-reference only; **cost sits in §1, not double-counted**.

---

## Subassembly PC-ASM-ELP-01 — Electronics & Power

### 6. Electronics & Power

> Control architecture: the DLC32 runs GRBL; a host PC connects over USB-B
> and acts as the MCP reasoning layer (Claude). CoreXY uses 2 motors, so
> 2 driver slots are populated (X + Y1). The Z slot is unused at this rev.

|   # | Part No.        | Description                   | Spec                                                  | Qty | Unit | Process | Supplier P/N    | Vendor | Link | Unit Price | Subtotal | Remarks                                                                      |
| --: | --------------- | ----------------------------- | ----------------------------------------------------- | --: | ---- | ------- | --------------- | ------ | ---- | ---------: | -------: | ---------------------------------------------------------------------------- |
|  24 | PC-ELP-DLC32    | Controller board MKS DLC32    | V2.1, ESP32, GRBL, 12–24 V, 90×70 mm                  |   1 | pc   | COTS    | MKS-DLC32-V2.1  | TBD    | TBD  |     110.00 |   110.00 | Spindle/TTL output drives solenoid (see ⚠ note below)                        |
|  25 | PC-ELP-TMC2209  | Stepper driver TMC2209        | Stepstick, UART-capable                               |   2 | pc   | COTS    | TMC2209-V2.0    | TBD    | TBD  |      12.00 |    24.00 | UART = manual flying-wire mod (see Electrical parameters)                    |
|  26 | PC-ELP-SOL-0530 | Solenoid Langshuoda LSD-0530B | Push-pull, DC 12 V, 0.8 A, 0.2–6 N, stroke 10 mm adj. |   1 | pc   | COTS    | LSD-0530B       | TBD    | TBD  |      18.00 |    18.00 | 60.5 mm L, 29 g, M3×2 mount; see ⚠ note below                                |
|  27 | PC-ELP-PSU-12   | Power supply 12 V DC          | 12 V, ≥ 5 A (60 W), enclosed                          |   1 | pc   | COTS    | LRS-50-12 / 60W | TBD    | TBD  |      35.00 |    35.00 | Sizing: 2×NEMA17 ~3 A + solenoid 0.8 A + board → ≥4 A; pick 5 A for headroom |
|  28 | PC-ELP-USBB     | USB-B cable                   | USB-A ↔ USB-B, 1.5 m                                  |   1 | pc   | COTS    | USB-AB-1M5      | TBD    | TBD  |       6.00 |     6.00 | DLC32 ↔ host PC (MCP server link)                                            |
|  29 | PC-ELP-DCJACK   | DC barrel / screw terminal    | 5.5×2.1 mm or terminal block                          |   1 | pc   | COTS    | DC-TERM-01      | TBD    | TBD  |       2.00 |     2.00 | PSU → DLC32 12 V input                                                       |
|  30 | PC-ELP-WIRE     | Hookup wire set               | 20 AWG, red/black, ~2 m                               |   1 | set  | COTS    | WIRE-20-RB      | TBD    | TBD  |       5.00 |     5.00 | Motor / power / solenoid leads                                               |

Section subtotal: **¥ 200.00**

> ### ⚠ Engineering note — solenoid drive (design decision)
>
> By explicit design decision, the LSD-0530B is wired **directly** to the
> DLC32 spindle/TTL output and controlled as a GRBL "spindle". A dedicated
> MOSFET driver and a flyback (freewheeling) diode are **intentionally NOT
> listed** in this BOM.
>
> Risk acknowledged and recorded (not silently omitted): the spindle/TTL
> pin is a logic-level PWM signal, not a power output. It may not source
> the solenoid's 0.8 A coil current, and coil turn-off produces a reverse
> EMF spike that — without a flyback diode — can damage the board output.
> If bench testing shows the solenoid does not actuate, or the port is
> stressed, add (a) a logic-level MOSFET switch module and (b) a flyback
> diode (e.g. 1N4007) across the coil.

---

## Summary

**Self-fabricated parts** (`Process = Print` or `Machine`):

| #   | Part                  | Process | Source / Status                       |
| --- | --------------------- | ------- | ------------------------------------- |
| 8   | Y-carriage plate      | Machine | TBD                                   |
| 9   | X-carriage (toolhead) | Machine | TBD                                   |
| 14  | Belt clamp            | Print   | `hardware/parts/custom/belt_clamp.py` |

Generated STEPs land in `hardware/output/step/` after `python -m hardware`.

**Outsourced (buy-to-print) parts:** §1 items 1–3 (frame extrusions) —
fabricated to our cut/machining spec by Jiangsu Ruizhou.

**Purchased subtotal (excl. standard parts):**

| Subassembly / Section              |          ¥ |
| ---------------------------------- | ---------: |
| CXY · 1. Frame (parts + machining) |      48.00 |
| CXY · 2. Motion                    |     130.00 |
| CXY · 3. Transmission              |      50.00 |
| CXY · 4. Drive                     |      96.00 |
| ELP · 6. Electronics & Power       |     200.00 |
| **Subtotal**                       | **524.00** |

**Standard parts:** estimate per fastener kit, fill in after sourcing.
(M6×25 already costed in §1 — exclude from the kit estimate to avoid
double-counting.)
**Total:** **¥ TBD**

---

## Field legend

| Field                     | Meaning                                                                                                    |
| ------------------------- | ---------------------------------------------------------------------------------------------------------- |
| `#`                       | Item number — matches drawing balloon callout                                                              |
| `Part No.`                | Unique part code (mandatory). Print/Machine = own drawing no.; standard = GB code                          |
| `Spec`                    | Size / variant detail                                                                                      |
| `Qty` / `Unit`            | Quantity and unit of issue (pc / set / length)                                                             |
| `Process`                 | How the part is obtained — see process legend below                                                        |
| `Material`                | Required for Print/Machine parts; drives process & cost                                                    |
| `Supplier P/N`            | Vendor's part number for reorder / reproducibility                                                         |
| `Vendor`                  | Who to buy it from. `Self` = fabricated, not purchased. `TBD` = not yet sourced (deliberately not guessed) |
| `Link`                    | Direct product URL. `TBD` until a real URL exists (no invented links)                                      |
| `Unit Price` / `Subtotal` | `Subtotal = Qty × Unit Price`                                                                              |
| `Remarks`                 | Cut size, surface finish, A/B-side notes, wiring                                                           |

**Process legend** — how each part is obtained:

| Value        | Meaning                                                            | Plan        |
| ------------ | ------------------------------------------------------------------ | ----------- |
| `Print`      | Self-fabricated, 3D printed. Has build123d source + STEP.          | Generate    |
| `Machine`    | Self-fabricated, machined (mill/CNC). Has build123d source + STEP. | Generate    |
| `COTS`       | Commercial off-the-shelf catalog part, bought as-is.               | Purchase    |
| `Standard`   | Standard hardware bought by spec (GB code), vendor-agnostic.       | Purchase    |
| `Outsourced` | Fabricated to our drawing by a subcontractor (buy-to-print).       | Subcontract |

Revision is kept in the file header (one source of truth), not per row.
This is a 2-level BOM: top assembly PC-ASM-TOP-01 → subassemblies
PC-ASM-CXY-01 (§1–5) + PC-ASM-ELP-01 (§6).

---

## Electrical parameters (verified / assumed)

| Item                                                                       | Source                                               |
| -------------------------------------------------------------------------- | ---------------------------------------------------- |
| DLC32: 4 driver slots, USB-B PC port, 12–24 V ≤10 A, 90×70 mm              | DLC32 V2.1 official manual / specs (web, 2026-05-17) |
| TMC2209 ×2 (CoreXY = X + Y1)                                               | CoreXY 2-motor topology                              |
| TMC2209 UART = flying-wire mod, not default on DLC32                       | DLC32 UART community docs (web)                      |
| LSD-0530B: DC 12 V, 0.8 A, 0.2–6 N, stroke 10 mm adj., 60.5 mm, 29 g, M3×2 | Supplier product page (user-supplied image)          |
| Prices on §6 rows                                                          | **ESTIMATES** — confirm at sourcing                  |
