import { beforeEach, describe, expect, it, vi } from "vitest";
import { act } from "react";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { DragEndEvent } from "@dnd-kit/core";
import type { GetQueueResponse, WorkQueue, WorkQueueItem } from "@multica/core/types";
import { renderWithI18n } from "../../test/i18n";
import { NavigationProvider, type NavigationAdapter } from "../../navigation";
import { QueueDetailPage } from "./queue-detail-page";

const mocks = vi.hoisted(() => ({
  data: null as GetQueueResponse | null,
  pauseQueue: vi.fn(),
  resumeQueue: vi.fn(),
  clearFinished: vi.fn(),
  reorderItems: vi.fn(),
  startQueue: vi.fn(),
  addItems: vi.fn(),
  updateItem: vi.fn(),
  deleteItem: vi.fn(),
  retryItem: vi.fn(),
  deleteQueue: vi.fn(),
}));

vi.mock("@tanstack/react-query", () => ({
  useQuery: (options: { queryKey?: readonly unknown[] }) => {
    const key = options.queryKey?.[0];
    if (key === "queue-detail") {
      return { data: mocks.data, isLoading: false };
    }
    return { data: undefined, isLoading: false };
  },
  queryOptions: (options: unknown) => options,
}));

vi.mock("@multica/core/queues", () => ({
  queueDetailOptions: () => ({ queryKey: ["queue-detail"] }),
  useAddQueueItems: () => ({ mutateAsync: mocks.addItems }),
  useClearFinishedQueueItems: () => ({ mutateAsync: mocks.clearFinished, isPending: false }),
  useDeleteQueueItem: () => ({ mutate: mocks.deleteItem }),
  usePauseQueue: () => ({ mutateAsync: mocks.pauseQueue, isPending: false }),
  useResumeQueue: () => ({ mutateAsync: mocks.resumeQueue, isPending: false }),
  useReorderQueueItems: () => ({ mutate: mocks.reorderItems }),
  useStartQueue: () => ({ mutateAsync: mocks.startQueue, isPending: false }),
  useUpdateQueueItem: () => ({ mutate: mocks.updateItem }),
  useRetryQueueItem: () => ({ mutate: mocks.retryItem, isPending: false }),
  useDeleteQueue: () => ({ mutateAsync: mocks.deleteQueue }),
}));

vi.mock("@multica/core/hooks", () => ({
  useWorkspaceId: () => "ws-1",
}));

vi.mock("@multica/core/paths", () => ({
  useWorkspacePaths: () => ({
    queues: () => "/test-workspace/queues",
    queueDetail: (id: string) => `/test-workspace/queues/${id}`,
    issueDetail: (id: string) => `/test-workspace/issues/${id}`,
  }),
}));

vi.mock("@multica/core/workspace/hooks", () => ({
  useActorName: () => ({
    getActorName: () => "Test Agent",
  }),
}));

vi.mock("sonner", () => ({
  toast: { error: vi.fn(), success: vi.fn() },
}));

// Pickers pull in agent/squad list queries and issue search — out of scope for
// this page's own logic, so stub them the same way create-issue.test.tsx stubs
// IssuePickerModal.
vi.mock("../../autopilots/components/pickers/agent-picker", () => ({
  AgentPicker: () => null,
}));

vi.mock("../../modals/issue-picker-modal", () => ({
  IssuePickerModal: () => null,
}));

vi.mock("../../common/actor-avatar", () => ({
  ActorAvatar: () => null,
}));

// Mock dnd-kit — same pattern as swimlane-view.test.tsx: capture onDragEnd so
// a reorder can be simulated without real pointer events, keep a real
// arrayMove since the production code's reorder assertion depends on it.
let lastOnDragEnd: ((event: DragEndEvent) => void) | null = null;

vi.mock("@dnd-kit/core", () => ({
  DndContext: ({ children, onDragEnd }: any) => {
    lastOnDragEnd = onDragEnd;
    return children;
  },
  PointerSensor: class {},
  useSensor: () => ({}),
  useSensors: () => [],
}));

vi.mock("@dnd-kit/sortable", () => ({
  SortableContext: ({ children }: any) => children,
  verticalListSortingStrategy: {},
  arrayMove: <T,>(arr: T[], from: number, to: number): T[] => {
    const copy = arr.slice();
    const [item] = copy.splice(from, 1);
    copy.splice(to, 0, item!);
    return copy;
  },
  useSortable: () => ({
    attributes: {},
    listeners: {},
    setNodeRef: vi.fn(),
    transform: null,
    transition: null,
    isDragging: false,
  }),
}));

vi.mock("@dnd-kit/utilities", () => ({
  CSS: { Transform: { toString: () => undefined } },
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

const ITEMS: WorkQueueItem[] = [
  {
    id: "item-1",
    queue_id: "queue-1",
    seq: 1,
    kind: "prompt",
    title: "First",
    body: null,
    issue_id: null,
    agent_id: null,
    status: "pending",
    task_id: null,
    error: null,
    started_at: null,
    finished_at: null,
  },
  {
    id: "item-2",
    queue_id: "queue-1",
    seq: 2,
    kind: "prompt",
    title: "Second",
    body: null,
    issue_id: null,
    agent_id: null,
    status: "pending",
    task_id: null,
    error: null,
    started_at: null,
    finished_at: null,
  },
  {
    id: "item-3",
    queue_id: "queue-1",
    seq: 3,
    kind: "prompt",
    title: "Third",
    body: null,
    issue_id: null,
    agent_id: null,
    status: "pending",
    task_id: null,
    error: null,
    started_at: null,
    finished_at: null,
  },
];

function makeAdapter(overrides: Partial<NavigationAdapter> = {}): NavigationAdapter {
  return {
    push: vi.fn(),
    replace: vi.fn(),
    back: vi.fn(),
    pathname: "/test-workspace/queues/queue-1",
    searchParams: new URLSearchParams(),
    getShareableUrl: (p) => p,
    ...overrides,
  };
}

function renderDetail(data: GetQueueResponse) {
  mocks.data = data;
  const adapter = makeAdapter();
  renderWithI18n(
    <NavigationProvider value={adapter}>
      <QueueDetailPage queueId="queue-1" />
    </NavigationProvider>,
  );
  return adapter;
}

beforeEach(() => {
  mocks.data = null;
  lastOnDragEnd = null;
  mocks.pauseQueue.mockClear();
  mocks.resumeQueue.mockClear();
  mocks.clearFinished.mockClear();
  mocks.reorderItems.mockClear();
  mocks.startQueue.mockClear();
  mocks.addItems.mockClear();
  mocks.updateItem.mockClear();
  mocks.deleteItem.mockClear();
  mocks.retryItem.mockClear();
  mocks.deleteQueue.mockClear();
  mocks.deleteQueue.mockResolvedValue(undefined);
});

describe("QueueDetailPage — verb buttons", () => {
  it("shows only Start for an idle queue", () => {
    renderDetail({ queue: { ...QUEUE, status: "idle" }, items: [] });

    expect(screen.getByRole("button", { name: "Start" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Pause" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Resume" })).not.toBeInTheDocument();
  });

  it("shows Pause for a scheduled queue (FIX-4)", () => {
    renderDetail({ queue: { ...QUEUE, status: "scheduled" }, items: [] });

    expect(screen.getByRole("button", { name: "Pause" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Start" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Resume" })).not.toBeInTheDocument();
  });

  it("shows Pause for a running queue", () => {
    renderDetail({ queue: { ...QUEUE, status: "running" }, items: [] });

    expect(screen.getByRole("button", { name: "Pause" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Start" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Resume" })).not.toBeInTheDocument();
  });

  it("shows only Resume for a paused queue", () => {
    renderDetail({ queue: { ...QUEUE, status: "paused" }, items: [] });

    expect(screen.getByRole("button", { name: "Resume" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Start" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Pause" })).not.toBeInTheDocument();
  });
});

describe("QueueDetailPage — reorder", () => {
  it("sends the full reordered id array to the reorder mutation", () => {
    renderDetail({ queue: QUEUE, items: ITEMS });

    expect(lastOnDragEnd).not.toBeNull();
    act(() => {
      lastOnDragEnd!({
        active: { id: "item-1" },
        over: { id: "item-3" },
      } as DragEndEvent);
    });

    expect(mocks.reorderItems).toHaveBeenCalledTimes(1);
    const [payload] = mocks.reorderItems.mock.calls[0]!;
    expect(payload).toEqual({ id: "queue-1", order: ["item-2", "item-3", "item-1"] });
  });
});

describe("QueueDetailPage — composer validation", () => {
  it("does not submit a prompt item with an empty title", async () => {
    renderDetail({ queue: QUEUE, items: [] });

    expect(screen.getByRole("button", { name: /Add/ })).toBeDisabled();
    expect(mocks.addItems).not.toHaveBeenCalled();
  });

  it("does not submit an issue item without a selected issue", async () => {
    const user = userEvent.setup();
    renderDetail({ queue: QUEUE, items: [] });

    await user.click(screen.getByRole("button", { name: "Issue" }));

    expect(screen.getByRole("button", { name: /Add/ })).toBeDisabled();
    expect(mocks.addItems).not.toHaveBeenCalled();
  });
});

describe("QueueDetailPage — retry failed item", () => {
  it("shows Retry only on failed items and calls the mutation", async () => {
    const user = userEvent.setup();
    renderDetail({
      queue: QUEUE,
      items: [
        ITEMS[0]!,
        { ...ITEMS[1]!, status: "failed", error: "boom" },
      ],
    });

    const retryButtons = screen.getAllByRole("button", { name: "Retry item" });
    expect(retryButtons).toHaveLength(1);

    await user.click(retryButtons[0]!);

    expect(mocks.retryItem).toHaveBeenCalledTimes(1);
    const [payload] = mocks.retryItem.mock.calls[0]!;
    expect(payload).toEqual({ id: "queue-1", itemId: "item-2" });
  });
});

describe("QueueDetailPage — delete queue", () => {
  it("awaits the delete then navigates back to the queues list", async () => {
    const user = userEvent.setup();
    const adapter = renderDetail({ queue: QUEUE, items: [] });

    await user.click(screen.getByRole("button", { name: "Delete queue" }));
    expect(await screen.findByText(/Delete "Backlog groomer"\?/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Delete" }));

    expect(mocks.deleteQueue).toHaveBeenCalledWith("queue-1");
    expect(adapter.push).toHaveBeenCalledWith("/test-workspace/queues");
  });

  it("warns about the in-flight task when the queue is running", async () => {
    const user = userEvent.setup();
    renderDetail({ queue: { ...QUEUE, status: "running" }, items: [] });

    await user.click(screen.getByRole("button", { name: "Delete queue" }));

    expect(await screen.findByText(/task will keep running/)).toBeInTheDocument();
  });
});
