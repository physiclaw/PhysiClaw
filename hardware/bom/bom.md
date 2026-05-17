# PhysiClaw — Full Machine BOM

> **Rev:** 0.8 · **Date:** 2026-05-17 · **BOM type:** eBOM (design stage)
> **Assembly P/N:** PC-ASM-TOP-01 · **Currency:** CNY · **Maintainer:** QIAOQIAN

Top-level machine BOM. The CoreXY frame + slider is subassembly
**PC-ASM-CXY-01** (sections 1–5); electronics & power is subassembly
**PC-ASM-ELP-01** (section 6).

---

## Subassembly PC-ASM-CXY-01 — CoreXY Frame + Slider

### 1. Frame

> All three extrusions are made-to-order — message the seller with the
> cut, tapping and counterbore specs before checkout. The linked SKU is
> the seller's generic profile listing. Ask the seller to include 8×
> M6×25 socket-head cap screws in the same shipment (paid separately,
> just saves ordering them elsewhere — covers §5 item 20).
>
> **Message to send the seller:**
>
> 1. 2040 profile, L 170 mm × 2, black anodized, tapped M6 both ends.
> 2. 2040 profile, L 335 mm × 2, black anodized, Ø6.6 through-hole +
>    Ø11 counterbore, 10 mm from each end.
> 3. 1020 profile, L 165 mm × 1, black anodized.
> 4. M6×25 socket-head cap screws × 8.

| #   | Part No.        | Description             | Spec                     | Qty | Unit | Material | Supplier P/N | Unit Price | Subtotal | Remarks                                               |
| --- | --------------- | ----------------------- | ------------------------ | --- | ---- | -------- | ------------ | ---------- | -------- | ----------------------------------------------------- |
| 1   | PC-FRM-2040-170 | Aluminum extrusion 2040 | L 170 mm, black anodized | 2   | pc   | Aluminum | [TDT-2040](https://item.taobao.com/item.htm?id=609835216661&skuId=4459722638632)     | 4.35       | 8.70     | Both ends tapped M6                                   |
| 2   | PC-FRM-2040-335 | Aluminum extrusion 2040 | L 335 mm, black anodized | 2   | pc   | Aluminum | [TDT-2040](https://item.taobao.com/item.htm?id=609835216661&skuId=4459722638632)     | 8.50       | 17.00    | Counterbore Ø6.6 through, Ø11 c'bore, 10 mm from ends |
| 3   | PC-FRM-1020-165 | Aluminum extrusion 1020 | L 165 mm, black anodized | 1   | pc   | Aluminum | [TDT-1020](https://item.taobao.com/item.htm?id=628103627886&skuId=4627298391628)     | 1.72       | 1.72     |                                                       |

Section subtotal: **¥ 50.00** (line-items ¥27.42 + machining ¥12 +
freight ¥10 + screws ¥1)

### 2. Motion

| #   | Part No.         | Description           | Spec               | Qty | Unit | Material | Supplier P/N | Unit Price | Subtotal | Remarks          |
| --- | ---------------- | --------------------- | ------------------ | --- | ---- | -------- | ------------ | ---------- | -------- | ---------------- |
| 6   | PC-MOT-MGN12-350 | Linear rail MGN12H    | L 350 mm, w/ block | 2   | set  | —        | MGN12H-350   | 45.00      | 90.00    | Y axis, A/B side |
| 7   | PC-MOT-MGN12-300 | Linear rail MGN12H    | L 300 mm, w/ block | 1   | set  | —        | MGN12H-300   | 40.00      | 40.00    | X axis (gantry)  |
| 8   | PC-SLD-YCAR-01   | Y-carriage plate      | Custom             | 2   | pc   | Al 6061  | —            | —          | —        |                  |
| 9   | PC-SLD-XCAR-01   | X-carriage (toolhead) | Custom             | 1   | pc   | Al 6061  | —            | —          | —        |                  |

Section subtotal: **¥ 130.00**

### 3. Transmission

| #   | Part No.       | Description           | Spec              | Qty | Unit   | Material     | Supplier P/N | Unit Price | Subtotal | Remarks                               |
| --- | -------------- | --------------------- | ----------------- | --- | ------ | ------------ | ------------ | ---------- | -------- | ------------------------------------- |
| 10  | PC-TRN-GT2-16  | Timing belt GT2-6 mm  | Open-end, ~1.6 m  | 2   | length | Rubber/fiber | GT2-6-OE     | 8.00       | 16.00    | Belt A / Belt B                       |
| 11  | PC-TRN-PUL-20T | Timing pulley 20T     | GT2, 5 mm bore    | 2   | pc     | Aluminum     | GT2-20T-5    | 6.00       | 12.00    | Motor shaft                           |
| 12  | PC-TRN-IDL-BIG | Idler pulley, toothed | 5 mm bore, w/ brg | 2   | pc     | —            | IDL-GT2-5    | 4.00       | 8.00     | Corner                                |
| 13  | PC-TRN-IDL-SML | Idler pulley, smooth  | 5 mm bore, w/ brg | 4   | pc     | —            | IDL-SM-5     | 3.50       | 14.00    | Turn points                           |
| 14  | PC-TRN-BCLP-01 | Belt clamp            | Custom            | 2   | pc     | PETG         | —            | —          | —        | `hardware/parts/custom/belt_clamp.py` |

Section subtotal: **¥ 50.00**

### 4. Drive

| #   | Part No.      | Description          | Spec           | Qty | Unit | Material | Supplier P/N | Unit Price | Subtotal | Remarks           |
| --- | ------------- | -------------------- | -------------- | --- | ---- | -------- | ------------ | ---------- | -------- | ----------------- |
| 15  | PC-DRV-NEMA17 | Stepper motor NEMA17 | 42-40, 1.5 A   | 2   | pc   | —        | 17HS4401     | 42.00      | 84.00    | Motor A / Motor B |
| 16  | PC-DRV-MNT-17 | Motor mount bracket  | NEMA17, L-type | 2   | pc   | Aluminum | MM-17-L      | 6.00       | 12.00    |                   |

Section subtotal: **¥ 96.00**

### 5. Standard Parts (GB) — roll-up

Listed once per GB code, grouped by family (screws → nuts → washers/rings).

| #   | Standard Code | Description           | Spec     | Qty | Unit | Remarks             |
| --- | ------------- | --------------------- | -------- | --- | ---- | ------------------- |
| 17  | GB/T 70.1     | Socket head cap screw | M3×8     | 30  | pc   | Carriage / brackets |
| 18  | GB/T 70.1     | Socket head cap screw | M5×16    | 16  | pc   | Frame / rail        |
| 19  | GB/T 70.1     | Socket head cap screw | M5×10    | 12  | pc   | Rail to extrusion   |
| 20  | GB/T 70.1     | Socket head cap screw | M6×25    | 8   | pc   | Frame end-fastening; ships w/ §1 order |
| 21  | GB/T 6170     | Hex nut               | M5       | 16  | pc   |                     |
| 22  | GB/T 818      | T-slot nut            | 2020, M5 | 28  | pc   | Extrusion fastening |
| 23  | GB/T 894      | Retaining ring        | Shaft 5  | 8   | pc   | Idler shafts        |
| 24  | —             | Rubber foot           | M6, D20  | 4   | pc   | Base feet           |

---

## Subassembly PC-ASM-ELP-01 — Electronics & Power

### 6. Electronics & Power

> Control architecture: the DLC32 runs GRBL; a host PC connects over USB-B
> and acts as the MCP reasoning layer (Claude). CoreXY uses 2 motors, so
> 2 driver slots are populated (X + Y1). The Z slot is unused at this rev.

| #   | Part No.        | Description                   | Spec                                                  | Qty | Unit | Supplier P/N    | Unit Price | Subtotal | Remarks                                                                      |
| --- | --------------- | ----------------------------- | ----------------------------------------------------- | --- | ---- | --------------- | ---------- | -------- | ---------------------------------------------------------------------------- |
| 25  | PC-ELP-DLC32    | Controller board MKS DLC32    | V2.1, ESP32, GRBL, 12–24 V, 90×70 mm                  | 1   | pc   | MKS-DLC32-V2.1  | 110.00     | 110.00   | Spindle/TTL output drives solenoid                                           |
| 26  | PC-ELP-TMC2209  | Stepper driver TMC2209        | Stepstick, UART-capable                               | 2   | pc   | TMC2209-V2.0    | 12.00      | 24.00    | UART = manual flying-wire mod                                                |
| 27  | PC-ELP-SOL-0530 | Solenoid Langshuoda LSD-0530B | Push-pull, DC 12 V, 0.8 A, 0.2–6 N, stroke 10 mm adj. | 1   | pc   | LSD-0530B       | 18.00      | 18.00    | 60.5 mm L, 29 g, M3×2 mount                                                  |
| 28  | PC-ELP-PSU-12   | Power supply 12 V DC          | 12 V, ≥ 5 A (60 W), enclosed                          | 1   | pc   | LRS-50-12 / 60W | 35.00      | 35.00    | Sizing: 2×NEMA17 ~3 A + solenoid 0.8 A + board → ≥4 A; pick 5 A for headroom |
| 29  | PC-ELP-USBB     | USB-B cable                   | USB-A ↔ USB-B, 1.5 m                                  | 1   | pc   | USB-AB-1M5      | 6.00       | 6.00     | DLC32 ↔ host PC (MCP server link)                                            |
| 30  | PC-ELP-DCJACK   | DC barrel / screw terminal    | 5.5×2.1 mm or terminal block                          | 1   | pc   | DC-TERM-01      | 2.00       | 2.00     | PSU → DLC32 12 V input                                                       |
| 31  | PC-ELP-WIRE     | Hookup wire set               | 20 AWG, red/black, ~2 m                               | 1   | set  | WIRE-20-RB      | 5.00       | 5.00     | Motor / power / solenoid leads                                               |

Section subtotal: **¥ 200.00**

---

## Summary

**Self-fabricated parts** (Spec = `Custom`):

| #   | Part                  | Method  | Source / Status                       |
| --- | --------------------- | ------- | ------------------------------------- |
| 8   | Y-carriage plate      | Machine | TBD                                   |
| 9   | X-carriage (toolhead) | Machine | TBD                                   |
| 14  | Belt clamp            | Print   | `hardware/parts/custom/belt_clamp.py` |

Generated STEPs land in `hardware/output/step/` after `python -m hardware`.

**Outsourced (buy-to-print) parts:** §1 items 1–3 (frame extrusions) —
fabricated to our cut/machining spec by an outsource supplier.

**Purchased subtotal (excl. standard parts):**

| Subassembly / Section              | ¥          |
| ---------------------------------- | ---------- |
| CXY · 1. Frame (supplier quote)    | 50.00      |
| CXY · 2. Motion                    | 130.00     |
| CXY · 3. Transmission              | 50.00      |
| CXY · 4. Drive                     | 96.00      |
| ELP · 6. Electronics & Power       | 200.00     |
| **Subtotal**                       | **526.00** |

**Standard parts:** estimate per fastener kit, fill in after sourcing.
**Total:** **¥ TBD**
