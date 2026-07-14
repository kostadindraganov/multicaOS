import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { I18nProvider } from "@multica/core/i18n/react";
import enCommon from "../locales/en/common.json";
import enModals from "../locales/en/modals.json";
import { IssuePickerModal } from "./issue-picker-modal";
import type { Issue } from "@multica/core/types";

const mockListIssues = vi.hoisted(() => vi.fn());
const mockSearchIssues = vi.hoisted(() => vi.fn());

vi.mock("@multica/core/api", () => ({
  api: {
    listIssues: mockListIssues,
    searchIssues: mockSearchIssues,
  },
}));

const TEST_RESOURCES = { en: { common: enCommon, modals: enModals } };

function makeIssue(id: string, title: string): Issue {
  return { id, identifier: `MUL-${id}`, title, status: "todo" } as Issue;
}

function renderPicker(props: Partial<React.ComponentProps<typeof IssuePickerModal>> = {}) {
  return render(
    <I18nProvider locale="en" resources={TEST_RESOURCES}>
      <IssuePickerModal
        open
        onOpenChange={vi.fn()}
        title="Pick issue"
        description="Pick an issue"
        excludeIds={[]}
        onSelect={vi.fn()}
        {...props}
      />
    </I18nProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  mockListIssues.mockResolvedValue({ issues: [], total: 0 });
  mockSearchIssues.mockResolvedValue({ issues: [], total: 0 });
});

describe("IssuePickerModal", () => {
  it("stays search-only by default: no fetch until typing", async () => {
    renderPicker();
    expect(screen.getByText("Type to search issues")).toBeInTheDocument();
    expect(mockListIssues).not.toHaveBeenCalled();
    expect(mockSearchIssues).not.toHaveBeenCalled();
  });

  it("preloadAll renders issues without typing", async () => {
    mockListIssues.mockResolvedValue({
      issues: [makeIssue("1", "First issue"), makeIssue("2", "Second issue")],
      total: 2,
    });
    renderPicker({ preloadAll: true, excludeIds: ["2"] });

    await waitFor(() => expect(screen.getByText("First issue")).toBeInTheDocument());
    expect(mockListIssues).toHaveBeenCalledWith({ limit: 50 });
    // excludeIds applies to the preloaded list too.
    expect(screen.queryByText("Second issue")).not.toBeInTheDocument();
    expect(mockSearchIssues).not.toHaveBeenCalled();
  });

  it("multiple toggles rows and returns them via onSelectMany", async () => {
    const user = userEvent.setup();
    const onSelectMany = vi.fn();
    const onSelect = vi.fn();
    const onOpenChange = vi.fn();
    mockListIssues.mockResolvedValue({
      issues: [makeIssue("1", "First issue"), makeIssue("2", "Second issue")],
      total: 2,
    });
    renderPicker({ preloadAll: true, multiple: true, onSelectMany, onSelect, onOpenChange });

    await waitFor(() => expect(screen.getByText("First issue")).toBeInTheDocument());

    await user.click(screen.getByText("First issue"));
    await user.click(screen.getByText("Second issue"));
    // Rows toggle instead of closing.
    expect(onSelect).not.toHaveBeenCalled();
    expect(onOpenChange).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Add 2 issues" }));
    expect(onSelectMany).toHaveBeenCalledWith([
      expect.objectContaining({ id: "1" }),
      expect.objectContaining({ id: "2" }),
    ]);
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("single-select still closes and returns one issue", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    const onOpenChange = vi.fn();
    mockListIssues.mockResolvedValue({ issues: [makeIssue("1", "First issue")], total: 1 });
    renderPicker({ preloadAll: true, onSelect, onOpenChange });

    await waitFor(() => expect(screen.getByText("First issue")).toBeInTheDocument());
    await user.click(screen.getByText("First issue"));
    expect(onSelect).toHaveBeenCalledWith(expect.objectContaining({ id: "1" }));
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });
});
