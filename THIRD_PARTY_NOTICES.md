# Third-party notices

Food Console is licensed under the MIT License (see [LICENSE](LICENSE)). It includes code
adapted from the project below, which is used under its own licence, reproduced in full.

## hereisSwapnil/blinkit-mcp

- Source: https://github.com/hereisSwapnil/blinkit-mcp
- Used in: `src/food_console/blinkit/parse.py` - the Blinkit order-history parsers
  (`parse_history_summary`, `split_qty`, `parse_history_detail`) are adapted from
  `src/order/services/history.py` of that project. The rest of `src/food_console/blinkit/`
  is a separate implementation of the same approach (driving blinkit.com with Playwright).

```
MIT License

Copyright (c) 2024 Swapnil Singh

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Dependencies

Packages installed from PyPI or npm (for example `anthropic`, `mcp`, `mcp-remote`,
`playwright`, `segno`, `starlette`) are not copied into this repository; each is
distributed under its own licence by its authors.
