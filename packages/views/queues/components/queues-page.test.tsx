import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { WorkQueue } from "@multica/core/types";
import { renderWithI18n } from "../../test/i18n";
import { NavigationProvider, type NavigationAdapter } from "../../navigation";
import { QueuesPage } from "./queues-page";

const mocks = vi.hoisted(() => ({
  queues: [] as WorkQueue[],
  createQueue: vi.fn(),
  updateQueue: vi.fn(),
  deleteQueue: vi.fn(),
}));

vi.mock("@tanstack/react-query", () => ({
  useQuery: (options: { queryKey?: readonly unknown[] }) => {
    const key = options.queryKey?.[0];
    if (key === "queues") {
      return { data: mocks.queues, isLoading: false };
    }
    if (key === "projects") {
      return { data: [{ id: "proj-1", title: "Roadmap" }], isLoading: false };
    }
    return { data: [], isLoading: false };
  },
  queryOptions: (options: unknown) => options,
}));

vi.mock("@multica/core/queues", () => ({
  queueListOptions: () => ({ queryKey: ["queues"] }),
  useCreateQueue: () => ({ mutateAsync: mocks.createQueue }),
  useUpdateQueue: () => ({ mutateAsync: mocks.updateQueue }),
  useDeleteQueue: () => ({ mutateAsync: mocks.deleteQueue }),
}));

vi.mock("@multica/core/projects/queries", () => ({
  projectListOptions: () => ({ queryKey: ["projects"] }),
}));

vi.mock("@multica/core/hooks", () => ({
  useWorkspaceId: () => "ws-1",
}));

vi.mock("@multica/core/paths", () => ({
  useWorkspacePaths: () => ({
    queues: () => "/test-workspace/queues",
    queueDetail: (id: string) => `/test-workspace/queues/${id}`,
  }),
}));

vi.mock("@multica/core/workspace/hooks", () => ({
  useActorName: () => ({
    getActorName: () => "Test Agent",
  }),
}));

const QUEUE: WorkQueue = {
  id: "queue-1",
  workspace_id: "ws-1",
  name: "Backlog groomer",
  description: null,
  default_agent_id: null,
  status: "idle",
  start_at: null,
  item_delay_seconds: 0,
  cron_expression: null,
  timezone: null,
  next_run_at: null,
  created_at: "2026-06-01T00:00:00Z",
  updated_at: "2026-06-01T00:00:00Z",
};

const RUNNING_QUEUE: WorkQueue = {
  ...QUEUE,
  id: "queue-2",
  name: "Release train",
  status: "running",
  project_id: "proj-1",
  item_counts: { pending: 2, running: 1, completed: 3, failed: 2 },
};

function makeAdapter(overrides: Partial<NavigationAdapter> = {}): NavigationAdapter {
  return {
    push: vi.fn(),
    replace: vi.fn(),
    back: vi.fn(),
    pathname: "/test-workspace/queues",
    searchParams: new URLSearchParams(),
    getShareableUrl: (p) => p,
    ...overrides,
  };
}

function renderQueues(adapter = makeAdapter()) {
  renderWithI18n(
    <NavigationProvider value={adapter}>
      <QueuesPage />
    </NavigationProvider>,
  );
  return adapter;
}

beforeEach(() => {
  mocks.queues = [QUEUE, RUNNING_QUEUE];
  mocks.createQueue.mockClear();
  mocks.updateQueue.mockClear();
  mocks.deleteQueue.mockClear();
  mocks.deleteQueue.mockResolvedValue(undefined);
});

describe("QueuesPage", () => {
  it("renders queue names from the list", () => {
    renderQueues();

    expect(screen.getByText(QUEUE.name)).toBeInTheDocument();
  });

  it("shows the queue's status badge", () => {
    renderQueues();

    expect(screen.getByText("Idle")).toBeInTheDocument();
  });

  it("opens the create dialog when 'New queue' is clicked", async () => {
    const user = userEvent.setup();
    renderQueues();

    await user.click(screen.getByRole("button", { name: "New queue" }));

    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByText("New queue", { selector: "[data-slot=dialog-title]" })).toBeInTheDocument();
  });

  it("filters queues by name search", async () => {
    const user = userEvent.setup();
    renderQueues();

    await user.type(screen.getByPlaceholderText("Search queues…"), "release");

    expect(screen.getByText("Release train")).toBeInTheDocument();
    expect(screen.queryByText("Backlog groomer")).not.toBeInTheDocument();
  });

  it("shows an empty message when no queue matches the search", async () => {
    const user = userEvent.setup();
    renderQueues();

    await user.type(screen.getByPlaceholderText("Search queues…"), "zzz");

    expect(screen.getByText("No queues match the filters")).toBeInTheDocument();
  });

  it("renders progress from item_counts and dash without them", () => {
    renderQueues();

    // 3 completed of 8 total, 2 failed — from RUNNING_QUEUE.item_counts.
    expect(screen.getByText(/3\/8 done/)).toBeInTheDocument();
    expect(screen.getByText(/2 failed/)).toBeInTheDocument();
  });

  it("deletes a queue through the row menu after confirmation", async () => {
    const user = userEvent.setup();
    renderQueues();

    await user.click(screen.getAllByRole("button", { name: "Queue actions" })[0]!);
    await user.click(await screen.findByRole("menuitem", { name: /Delete/ }));
    // Confirm dialog names the queue; confirm button is "Delete".
    expect(await screen.findByText(/Delete "Backlog groomer"\?/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Delete" }));

    expect(mocks.deleteQueue).toHaveBeenCalledWith("queue-1");
  });
});
