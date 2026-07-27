```markdown
# multicaOS Development Patterns

> Auto-generated skill from repository analysis

## Overview

This skill teaches you how to contribute to the `multicaOS` codebase, a TypeScript project (with Go backend) designed for full-stack development without a major framework. You'll learn the project's coding conventions, how to evolve the database and API, add features, localize the UI, build frontend components, and write effective tests. Step-by-step workflows and code examples are provided to help you follow established patterns and maintain code quality.

## Coding Conventions

- **File Naming:**  
  Use kebab-case for files.  
  _Example:_  
  ```
  user-profile.ts
  api-client.ts
  ```

- **Imports:**  
  Use relative imports.  
  _Example:_  
  ```typescript
  import { fetchUser } from './user-service';
  ```

- **Exports:**  
  Use named exports.  
  _Example:_  
  ```typescript
  export function fetchUser(id: string) { ... }
  export const USER_ROLE = 'admin';
  ```

- **Commit Messages:**  
  Follow [Conventional Commits](https://www.conventionalcommits.org/).  
  Prefixes: `fix`, `feat`, `docs`, `test`  
  _Example:_  
  ```
  feat(api): add support for custom issue properties
  fix(ui): correct tab navigation bug in dashboard
  ```

## Workflows

### Add or Evolve Database Table or Column
**Trigger:** When you need to add a new database table, column, or change schema (e.g., for a new feature or property).  
**Command:** `/new-table`

1. Edit or add SQL migration files in `server/migrations/` (e.g., `.up.sql`/`.down.sql`).
2. Update SQL query definitions in `server/pkg/db/queries/*.sql`.
3. Regenerate Go code for queries and models in `server/pkg/db/generated/*.go`.
4. Update related Go structs or service logic in:
   - `server/internal/handler/`
   - `server/internal/service/`
   - `server/pkg/db/generated/`
5. Add or update tests for new schema or handler logic.
6. Update API schemas/types if exposed (e.g., `packages/core/api/schemas.ts`, `packages/core/types/*.ts`).

_Example migration:_
```sql
-- server/migrations/20230601_add_user_table.up.sql
CREATE TABLE users (
  id SERIAL PRIMARY KEY,
  name TEXT NOT NULL,
  email TEXT UNIQUE NOT NULL
);
```

### Add or Evolve API Endpoint or CRUD
**Trigger:** When you want to add a new API endpoint or extend an existing one (e.g., for new resource CRUD).  
**Command:** `/new-api-endpoint`

1. Add or update handler in `server/internal/handler/*.go`.
2. Update or add service logic in `server/internal/service/*.go`.
3. Update router in `server/cmd/server/router.go`.
4. Update or add CLI command in `server/cmd/multica/*.go`.
5. Update API client/types in `packages/core/api/client.ts` and `packages/core/types/*.ts`.
6. Update or add frontend hooks/queries if needed (e.g., `packages/core/*/queries.ts`).
7. Add or update tests for handler, CLI, and client.

_Example TypeScript client update:_
```typescript
// packages/core/api/client.ts
export async function fetchUsers() {
  return api.get('/users');
}
```

### Feature Development (Full Stack)
**Trigger:** When adding a new product feature or major enhancement (e.g., custom issue properties, new UI tab, new runtime support).  
**Command:** `/feature`

1. **Backend:** Add/modify migrations, handlers, services, and API types.
2. **Core:** Update types, queries, mutations, stores, and client logic.
3. **Frontend:** Add/modify React components, hooks, and UI logic.
4. **Localization:** Update JSON locale files for all supported languages.
5. **Testing:** Add/modify tests for backend, core, and frontend.
6. **Documentation:** Update docs and product overview files if user-facing.

_Example React component:_
```tsx
// packages/views/user/components/user-profile.tsx
export function UserProfile({ user }) {
  return <div>{user.name}</div>;
}
```

### Add or Update Localization
**Trigger:** When adding a new UI feature, label, or message that requires translation.  
**Command:** `/localize`

1. Edit or add keys in `packages/views/locales/en/*.json`.
2. Propagate changes to `packages/views/locales/ja/*.json`, `ko/*.json`, `zh-Hans/*.json`, etc.
3. Update or add tests for i18n coverage if needed.

_Example localization entry:_
```json
// packages/views/locales/en/common.json
{
  "user_profile": "User Profile"
}
```

### Add or Evolve Frontend UI Component
**Trigger:** When you want to add a new UI component or update an existing one (e.g., new picker, tab, or dialog).  
**Command:** `/new-ui-component`

1. Add or modify React component in `packages/views/**/components/*.tsx`.
2. Update or add associated test in `packages/views/**/components/*.test.tsx`.
3. Update localization files for new labels/messages.
4. Wire up to store/hooks if stateful.

_Example component and test:_
```tsx
// packages/views/dashboard/components/status-badge.tsx
export function StatusBadge({ status }) {
  return <span>{status}</span>;
}
```
```tsx
// packages/views/dashboard/components/status-badge.test.tsx
import { render } from '@testing-library/react';
import { StatusBadge } from './status-badge';

test('renders status', () => {
  const { getByText } = render(<StatusBadge status="Active" />);
  expect(getByText('Active')).toBeInTheDocument();
});
```

### Add or Update E2E or Regression Tests
**Trigger:** When adding a new feature or fixing a bug that needs automated test coverage.  
**Command:** `/add-test`

1. Add or update test files in `e2e/*.spec.ts` or `packages/views/**/test.ts(x)`.
2. Update fixtures or test utilities if needed.
3. Verify test coverage for the new/changed behavior.

_Example Vitest test:_
```typescript
// packages/core/utils/date.test.ts
import { formatDate } from './date';

test('formats date as YYYY-MM-DD', () => {
  expect(formatDate(new Date('2023-01-01'))).toBe('2023-01-01');
});
```

## Testing Patterns

- **Framework:** [Vitest](https://vitest.dev/)
- **Test File Pattern:** `*.test.ts` or `*.test.tsx`  
  _Example:_ `user-service.test.ts`, `status-badge.test.tsx`
- **Test Structure:**  
  Use `test()` or `describe()`/`it()` blocks.
- **Location:**  
  - Frontend: alongside components or in `__tests__` directories
  - Backend (Go): `_test.go` files alongside source

_Example:_
```typescript
// packages/core/utils/math.test.ts
import { add } from './math';

test('adds two numbers', () => {
  expect(add(2, 3)).toBe(5);
});
```

## Commands

| Command            | Purpose                                                      |
|--------------------|--------------------------------------------------------------|
| /new-table         | Add or evolve a database table or column                     |
| /new-api-endpoint  | Add or extend an API endpoint or CRUD operation              |
| /feature           | Start a full-stack feature development workflow              |
| /localize          | Add or update localization for new or changed UI features    |
| /new-ui-component  | Add or update a frontend UI component                        |
| /add-test          | Add or update end-to-end or regression tests                 |
```
