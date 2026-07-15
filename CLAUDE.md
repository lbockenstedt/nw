# Project Guidelines (CLAUDE.md)

This file contains project-specific technical guidelines to ensure consistency and efficiency.

## Build & Development
- **Build Command:** `npm run build` (Update this if different)
- **Install Dependencies:** `npm install`
- **Dev Server:** `npm run dev`

## Testing
- **Run All Tests:** `npm test`
- **Run Single Test:** `npm test -- <path_to_file>`
- **Test Conventions:** Use Vitest/Jest. Prefer integration tests for critical paths and unit tests for utility logic.

## Coding Standards
- **Style:** Match the existing style of the surrounding code.
- **Naming:** Follow camelCase for variables/functions, PascalCase for classes/components.
- **Documentation:** Add JSDoc to complex functions; keep comments concise and outcome-oriented.
- **Errors:** Use custom error classes for domain-specific failures.
