---
name: FixedPointMathLib
kind: library
node_id: src.utils.FixedPointMathLib:FixedPointMathLib
file: tests/fixtures/tier0_erc4626/src/utils/FixedPointMathLib.sol
lines: 7-255
loc: 249
cyclomatic_complexity: null
callers_count: 0
callees_count: 0
---

## Overview

FixedPointMathLib provides small, gas-efficient fixed-point arithmetic helpers (wad scale = 1e18) used for multiplying, dividing and exponentiation with correct rounding behavior. The library implements safe multiply/divide primitives with both downwards and upwards rounding (see mulDivDown/mulDivUp at lines 36-69) and higher-level convenience functions like mulWadDown/mulWadUp and divWadDown/divWadUp (lines 16-30). It also includes rpow (lines 71-158) for repeated exponentiation with a custom scalar, an integer sqrt implementation (lines 164-227) and a few unsafe helpers that return 0 instead of reverting on divide/modulo-by-zero (lines 229-254). The source is at tests/fixtures/tier0_erc4626/src/utils/FixedPointMathLib.sol (lines 7-255).

## Graph context

### Inheritance

_No inheritance edges._

### Implements

_Implements nothing._

### Uses

_No library uses._

### Callers

_No callers in this graph._

### Callees

_No callees in this graph._

## State

_Trailmark does not extract state variables yet — read the source at `tests/fixtures/tier0_erc4626/src/utils/FixedPointMathLib.sol` lines `7`-`255`._

## Functions

### External

_None._

### Public

_None._

### Internal

- [[libraries/FixedPointMathLib|FixedPointMathLib.mulWadDown]] `mulWadDown(uint256 x, uint256 y) -> uint256` — complexity 1 (callers: 0, callees: 1)
- [[libraries/FixedPointMathLib|FixedPointMathLib.mulWadUp]] `mulWadUp(uint256 x, uint256 y) -> uint256` — complexity 1 (callers: 0, callees: 1)
- [[libraries/FixedPointMathLib|FixedPointMathLib.divWadDown]] `divWadDown(uint256 x, uint256 y) -> uint256` — complexity 1 (callers: 0, callees: 1)
- [[libraries/FixedPointMathLib|FixedPointMathLib.divWadUp]] `divWadUp(uint256 x, uint256 y) -> uint256` — complexity 1 (callers: 0, callees: 1)
- [[libraries/FixedPointMathLib|FixedPointMathLib.mulDivDown]] `mulDivDown(uint256 x, uint256 y, uint256 denominator) -> uint256` — complexity 1 (callers: 4, callees: 0)
- [[libraries/FixedPointMathLib|FixedPointMathLib.mulDivUp]] `mulDivUp(uint256 x, uint256 y, uint256 denominator) -> uint256` — complexity 1 (callers: 4, callees: 0)
- [[libraries/FixedPointMathLib|FixedPointMathLib.rpow]] `rpow(uint256 x, uint256 n, uint256 scalar) -> uint256` — complexity 1 (callers: 0, callees: 0)
- [[libraries/FixedPointMathLib|FixedPointMathLib.sqrt]] `sqrt(uint256 x) -> uint256` — complexity 1 (callers: 0, callees: 0)
- [[libraries/FixedPointMathLib|FixedPointMathLib.unsafeMod]] `unsafeMod(uint256 x, uint256 y) -> uint256` — complexity 1 (callers: 0, callees: 0)
- [[libraries/FixedPointMathLib|FixedPointMathLib.unsafeDiv]] `unsafeDiv(uint256 x, uint256 y) -> uint256` — complexity 1 (callers: 0, callees: 0)
- [[libraries/FixedPointMathLib|FixedPointMathLib.unsafeDivUp]] `unsafeDivUp(uint256 x, uint256 y) -> uint256` — complexity 1 (callers: 0, callees: 0)

### Private

_None._

## Events / Errors / Modifiers

_Trailmark does not extract events, errors, or modifiers yet — read the source at `tests/fixtures/tier0_erc4626/src/utils/FixedPointMathLib.sol` lines `7`-`255`._

## Annotations

_No annotations yet._

## Risks

_No risks recorded._
