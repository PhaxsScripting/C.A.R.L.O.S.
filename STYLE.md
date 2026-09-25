# Phax style

Keep related work together: assistant code in Carlos, tracking in HoloHand, and
phone endpoints beside the phone client. Prefer small helpers, early returns,
and comments that explain why a decision exists.

The style takes inspiration from DrPerkyLegit's LegacyCord: straightforward
classes, attached braces, and short informal comments. No LegacyCord source is
included or relicensed here.

Use four spaces and 100 columns in Python and C++. Classes use PascalCase;
native methods and variables use camelCase where consistent with their module.
Python keeps snake_case functions so existing integrations remain readable.
Don't rename wire fields, permission IDs, or compatibility commands for a joke.

Owner flavor belongs in bounded internal helpers: `PhaxEventBus`, `GiggleGuard`,
`GiggleBudget`, and `BigBootyBudget`. Name security checks and user-facing errors
plainly. Comments can be casual, but must explain the code and stay useful.

Use Black for Python and clang-format for native source. Leave vendored code and
its attribution intact. Formatting is not an excuse to change behavior.
