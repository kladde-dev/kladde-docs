---
title: Floats as decimals
---

**Status: a measured idea, not a proposal.**

**Packing a float as the shortest decimal that reads back as it — its significant digits and a power of ten — would shrink the numbers of SVG drawings from 4 bytes to between 1.5 and 2.8, where binary schemes that encode one float at a time come out larger than a plain `f32`.**
In a [packed place](../spec/schema/type-descriptors.md#places), `f32` and `f64` keep their IEEE bytes, as they do in a slotted one, while integers become varints.
A decimal encoding would make floats variable-size too.
In memory they would stay what the application declares in any case; only the bytes on file would change, as they do for integers.

## What it would save

**On kladde-svg's corpus, the shortest decimal takes 1.54 to 2.78 bytes per number, and the two binary schemes 2.64 to 4.89, more than four bytes on every drawing.**
The numbers are every `f32` that kladde-svg's model stores for the drawings: coordinates, lengths, transforms, opacities and view boxes.
They were measured with a throwaway program, not kept, on three drawings and on resvg's test suite, whose files are small and whose numbers are mostly integers.

| bytes per number | tiger | coat of arms | world map | resvg's tests |
| --- | --- | --- | --- | --- |
| numbers | 11,001 | 22,422 | 168,554 | 34,072, in 1,800 files |
| share that are integers | 19 % | 9 % | 2 % | 90 % |
| fixed `f32` | 4.00 | 4.00 | 4.00 | 4.00 |
| byte-reversed bits, then a varint | 4.31 | 4.57 | 4.83 | 2.64 |
| a tag, then `f16` where exact, else `f32` | 4.39 | 4.64 | 4.89 | 2.84 |
| shortest decimal | 2.12 | 2.78 | 2.38 | 1.54 |

The decimal encoding measured is one LEB128 varint holding `zigzag(digits) × 16 + (exponent + 8)` for a power of ten from $10^{-8}$ to $10^{7}$, and otherwise an escape followed by the four IEEE bytes, five bytes in all.
On the world map, 4.5 % of the numbers take one byte, 55 % two, 38 % three, and 2 % four or five.
The byte-reversed variant is what Go's `encoding/gob` does: reversing the bytes puts a float's trailing zero mantissa bits first, where a varint drops them.
The tagged variant follows CBOR's preferred serialization, with zero in the tag alone.

**The binary schemes lose because SVG's numbers are short decimals, whose binary mantissas are full.**
A float's trailing mantissa bits are zero only for integers and fractions with a power of two below them, such as halves and quarters; `12.35` or `0.1` fill all 23 bits.
On resvg's tests, nine tenths of whose numbers are integers, the binary schemes do save, but less than the decimal one does.
The decimal encoding keeps what the source wrote, and that is short whenever the source's text was.

## What others do

**Encodings of one float at a time, the kind a packed place needs, are rare, and the decimal ones among them come from columnar databases.**

- *Go's gob* reverses a float's bytes before writing it as an integer; 17.0 takes three bytes.
- *CBOR's preferred serialization* ([RFC 8949](https://www.rfc-editor.org/rfc/rfc8949)) writes a float at the shortest of half, single and double precision that holds it exactly; *Amazon Ion* writes zero in no bytes, and other floats in four or eight.
- *SQLite* writes a `REAL` without a fractional part as an integer of one to eight bytes, which is the decimal encoding restricted to a power of ten of one.
- *Ion's decimal type* stores a coefficient and an exponent, each of variable length.
- *BtrBlocks*' pseudodecimal encoding (SIGMOD 2023) splits a double into its significant digits and a power of ten, with exceptions for those that do not split, and *ALP* (SIGMOD 2024, used in DuckDB) finds the doubles that are short decimals by scaling them to integers.
  Both compress columns rather than single values, but the split itself would work one value at a time.

**Compressors of float sequences reach further, and would not fit, since each value's encoding depends on the values before it.**
*Gorilla* (VLDB 2015) writes each value as its XOR with the one before; *Chimp* (VLDB 2022) and *Elf* (VLDB 2023) refine it, Elf by erasing the mantissa bits beyond a value's decimal precision first.
*FPC* predicts each value from earlier ones, *zfp* and *fpzip* compress arrays of scientific data, and Parquet's byte-stream split regroups a column's bytes for a general-purpose compressor.
Under any of them, changing one number would change the encoding of the numbers after it, so a mutation that kladde makes as one `Write` would rewrite a run of values.
They belong with [compressing data pages](../impl/packed-values.md#what-was-considered-and-declined), which could use them, rather than with packing.

## What adopting it would take

- **A canonical rule.**
  The specification would have to fix which digits a float is written with: the shortest decimal that reads back as the float, the closest such if there are several, and how a tie between two equally close ones breaks, as the Ryū algorithm and its ports do in most languages.
- **Escapes** for what has no short decimal: infinities, NaNs with their payloads, negative zero, and exponents outside the encoding's range.
  The prototype above wrote negative zero as zero, which a real encoding must not.
- **Time.**
  A store would compute a float's shortest decimal, and a load would parse one back with correct rounding; for the world map's 169,000 numbers, that should be milliseconds against the 78 ms its open took when this was measured.
- **No gain for computed numbers.**
  A coordinate that a rotation or a scaling produced has up to nine significant digits and takes the escape, five bytes, one more than an `f32`.
- **More size changes.**
  A float would become variable-size, so moving a point, an in-place `Write` while floats keep their IEEE bytes, would change the point's size whenever its digits change in number.
  A field that is moved often could be declared slotted, as a frequently changed integer can.
- **Tuning.**
  The varint's layout above is a first guess; giving integers a one-byte form, or fitting the exponent's range to where SVG's numbers lie, would likely save more.

**It might also settle whether an application should declare `f32` or `f64`.**
The shortest decimal of the `f64` that `12.35` parses to is `12.35`, as it is for the `f32`, so for numbers that came from decimal text, an `f64` should take about as many bytes on file as an `f32`, and its extra precision would cost space only where it is used.
That has not been measured.
