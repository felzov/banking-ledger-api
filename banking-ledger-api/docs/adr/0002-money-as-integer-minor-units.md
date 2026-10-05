# ADR 0002: Money as integer minor units

- Status: Accepted
- Date: 2026-10-05

## Context

Floating point cannot represent most decimal fractions exactly (`0.1 + 0.2 != 0.3`), so it is
never acceptable for money. The two correct candidates are integer minor units and `Decimal`.

| Criterion | Integer minor units | Decimal / NUMERIC |
|---|---|---|
| Exactness | Exact, no rounding context | Exact, but scale must be managed (`Decimal("10.005")` exceeds GBP precision unless quantized) |
| Database | `BIGINT`: native, fast, easy CHECK constraints | `NUMERIC`: native, slower, larger |
| API | `1050` is unambiguous but less readable | `"10.50"` is readable but must be a JSON string |
| Fractional values (FX rates, interest) | Not representable | Natural fit |

## Decision

- Store amounts as `BIGINT` minor units (pence, cents). Use Python `int` inside a `Money` value
  object that carries its currency.
- Expose amounts in the API as an integer field named `amount_minor`.
- Take the currency from the account's ledger, with an ISO 4217 exponent table (GBP=2, EUR=2).

## Consequences

- No conversion code between API, domain and database, so no conversion bugs.
- Clients must format for display themselves.
- FX or interest accrual would need `Decimal` for rates. Both are out of scope for the MVP.
- Risk: a client sends major units (`10` meaning £10). Mitigation: the explicit `amount_minor`
  name, strict integer validation (`10.5` is rejected), and an upper limit per transaction.
