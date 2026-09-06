import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SourcesPanel } from "../src/components/sources-panel";
import {
  ApiError,
  archiveSource,
  createSource,
  deleteSourceSchedule,
  getRun,
  getSource,
  listRuns,
  listSources,
  triggerSourceRun,
  unarchiveSource,
  upsertSourceSchedule,
  updateSourceStatus,
} from "../src/lib/api";
import type { Run, Source } from "../src/lib/types";

vi.mock("../src/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/lib/api")>();

  return {
    ...actual,
    archiveSource: vi.fn(),
    createSource: vi.fn(),
    deleteSourceSchedule: vi.fn(),
    getRun: vi.fn(),
    getSource: vi.fn(),
    listRuns: vi.fn(),
    listSources: vi.fn(),
    triggerSourceRun: vi.fn(),
    unarchiveSource: vi.fn(),
    upsertSourceSchedule: vi.fn(),
    updateSourceStatus: vi.fn(),
  };
});

const mockedArchiveSource = vi.mocked(archiveSource);
const mockedCreateSource = vi.mocked(createSource);
const mockedDeleteSourceSchedule = vi.mocked(deleteSourceSchedule);
const mockedGetSource = vi.mocked(getSource);
const mockedGetRun = vi.mocked(getRun);
const mockedListRuns = vi.mocked(listRuns);
const mockedListSources = vi.mocked(listSources);
const mockedTriggerSourceRun = vi.mocked(triggerSourceRun);
const mockedUnarchiveSource = vi.mocked(unarchiveSource);
const mockedUpsertSourceSchedule = vi.mocked(upsertSourceSchedule);
const mockedUpdateSourceStatus = vi.mocked(updateSourceStatus);

const existingSource: Source = {
  id: "source-existing",
  url: "http://mock-store:4000/products/existing",
  normalized_url: "http://mock-store:4000/products/existing",
  adapter_type: "mock_store",
  status: "active",
  created_at: "2026-08-29T00:00:00Z",
  updated_at: "2026-08-29T00:00:00Z",
};

const addedSource: Source = {
  id: "source-added",
  url: "http://mock-store:4000/products/added",
  normalized_url: "http://mock-store:4000/products/added",
  adapter_type: "mock_store",
  status: "paused",
  created_at: "2026-08-29T00:01:00Z",
  updated_at: "2026-08-29T00:01:00Z",
};

const completedRun: Run = {
  id: "run-1",
  source_id: existingSource.id,
  schedule_id: null,
  status: "completed",
  triggered_by: "manual",
  total_tasks: 1,
  succeeded_tasks: 1,
  failed_tasks: 0,
  started_at: "2026-08-29T00:02:00Z",
  finished_at: "2026-08-29T00:02:01Z",
  created_at: "2026-08-29T00:02:00Z",
};

const runningRun: Run = {
  ...completedRun,
  status: "running",
  succeeded_tasks: 0,
  finished_at: null,
};

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

beforeEach(() => {
  vi.resetAllMocks();
  mockedGetSource.mockImplementation(async ({ sourceId }) => ({
    ...(sourceId === existingSource.id ? existingSource : addedSource),
    schedule: null,
  }));
  mockedListRuns.mockResolvedValue({
    data: [],
    pagination: { next_cursor: null, has_more: false },
  });
});

describe("SourcesPanel", () => {
  it("loads and renders workspace sources", async () => {
    mockedListSources.mockResolvedValue({
      data: [existingSource],
      pagination: { next_cursor: null, has_more: false },
    });

    render(<SourcesPanel token="test-token" />);

    expect(await screen.findByText(existingSource.url)).toBeInTheDocument();
    expect(screen.getByText("Active")).toBeInTheDocument();
    expect(mockedListSources).toHaveBeenCalledWith("test-token");
  });

  it("shows an empty state when the workspace has no sources", async () => {
    mockedListSources.mockResolvedValue({
      data: [],
      pagination: { next_cursor: null, has_more: false },
    });

    render(<SourcesPanel token="test-token" />);

    expect(await screen.findByText("No sources yet")).toBeInTheDocument();
  });

  it("shows an existing schedule and its next run", async () => {
    mockedListSources.mockResolvedValue({
      data: [existingSource],
      pagination: { next_cursor: null, has_more: false },
    });
    mockedGetSource.mockResolvedValue({
      ...existingSource,
      schedule: {
        id: "schedule-1",
        source_id: existingSource.id,
        interval_minutes: 60,
        is_active: true,
        next_run_at: "2026-08-30T12:00:00Z",
        last_run_at: null,
        created_at: "2026-08-30T11:00:00Z",
        updated_at: "2026-08-30T11:00:00Z",
      },
    });

    render(<SourcesPanel token="test-token" />);

    expect(await screen.findByText("Runs every 60 minutes")).toBeInTheDocument();
    expect(screen.getByText(/Next run:/)).toBeInTheDocument();
    expect(screen.queryByText("2026-08-30T12:00:00Z")).not.toBeInTheDocument();
  });

  it("removes an existing schedule", async () => {
    mockedListSources.mockResolvedValue({
      data: [existingSource],
      pagination: { next_cursor: null, has_more: false },
    });
    mockedGetSource.mockResolvedValue({
      ...existingSource,
      schedule: {
        id: "schedule-1",
        source_id: existingSource.id,
        interval_minutes: 60,
        is_active: true,
        next_run_at: "2026-08-30T12:00:00Z",
        last_run_at: null,
        created_at: "2026-08-30T11:00:00Z",
        updated_at: "2026-08-30T11:00:00Z",
      },
    });
    mockedDeleteSourceSchedule.mockResolvedValue();

    render(<SourcesPanel token="test-token" />);

    expect(await screen.findByRole("button", { name: "Remove schedule" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Remove schedule" }));

    await waitFor(() => {
      expect(mockedDeleteSourceSchedule).toHaveBeenCalledWith({
        token: "test-token",
        sourceId: existingSource.id,
      });
    });

    expect(await screen.findByText("No schedule configured")).toBeInTheDocument();
    expect(await screen.findByText("Schedule removed.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Remove schedule" })).not.toBeInTheDocument();
  });

  it("saves a 15-minute schedule for a source", async () => {
    mockedListSources.mockResolvedValue({
      data: [existingSource],
      pagination: { next_cursor: null, has_more: false },
    });
    mockedUpsertSourceSchedule.mockResolvedValue({
      id: "schedule-1",
      source_id: existingSource.id,
      interval_minutes: 15,
      is_active: true,
      next_run_at: "2026-08-30T12:15:00Z",
      last_run_at: null,
      created_at: "2026-08-30T12:00:00Z",
      updated_at: "2026-08-30T12:00:00Z",
    });

    render(<SourcesPanel token="test-token" />);

    await screen.findByText(existingSource.url);
    fireEvent.click(screen.getByRole("button", { name: "Save schedule" }));

    await waitFor(() => {
      expect(mockedUpsertSourceSchedule).toHaveBeenCalledWith({
        token: "test-token",
        sourceId: existingSource.id,
        intervalMinutes: 15,
      });
    });

    expect(await screen.findByText("Schedule saved.")).toBeInTheDocument();
  });

  it("clears source action feedback when starting a run", async () => {
    mockedListSources.mockResolvedValue({
      data: [existingSource],
      pagination: { next_cursor: null, has_more: false },
    });
    mockedUpsertSourceSchedule.mockResolvedValue({
      id: "schedule-1",
      source_id: existingSource.id,
      interval_minutes: 15,
      is_active: true,
      next_run_at: "2026-08-30T12:15:00Z",
      last_run_at: null,
      created_at: "2026-08-30T12:00:00Z",
      updated_at: "2026-08-30T12:00:00Z",
    });
    mockedTriggerSourceRun.mockResolvedValue({
      run_id: runningRun.id,
      task_id: "task-1",
      status: "pending",
    });
    mockedGetRun.mockResolvedValue(runningRun);

    render(<SourcesPanel token="test-token" />);

    await screen.findByText(existingSource.url);
    fireEvent.click(screen.getByRole("button", { name: "Save schedule" }));
    expect(await screen.findByText("Schedule saved.")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Run now" }));

    await waitFor(() => {
      expect(mockedTriggerSourceRun).toHaveBeenCalledWith({
        token: "test-token",
        sourceId: existingSource.id,
      });
    });

    expect(screen.queryByText("Schedule saved.")).not.toBeInTheDocument();
    expect(await screen.findByText(/Latest run: Running/)).toBeInTheDocument();
  });

  it("pauses an active source and shows the resume action", async () => {
    mockedListSources.mockResolvedValue({
      data: [existingSource],
      pagination: { next_cursor: null, has_more: false },
    });
    mockedUpdateSourceStatus.mockResolvedValue({
      ...existingSource,
      status: "paused",
    });

    render(<SourcesPanel token="test-token" />);

    await screen.findByText(existingSource.url);
    fireEvent.click(screen.getByRole("button", { name: "Pause" }));

    await waitFor(() => {
      expect(mockedUpdateSourceStatus).toHaveBeenCalledWith({
        token: "test-token",
        sourceId: existingSource.id,
        status: "paused",
      });
    });

    expect(await screen.findByText("Paused")).toBeInTheDocument();
    expect(await screen.findByText("Source paused.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Resume" })).toBeInTheDocument();
  });

  it("keeps an active source and shows an error when pausing fails", async () => {
    mockedListSources.mockResolvedValue({
      data: [existingSource],
      pagination: { next_cursor: null, has_more: false },
    });
    mockedUpdateSourceStatus.mockRejectedValue(
      new ApiError("Unable to pause source.", 500),
    );

    render(<SourcesPanel token="test-token" />);

    await screen.findByText(existingSource.url);
    fireEvent.click(screen.getByRole("button", { name: "Pause" }));

    await waitFor(() => {
      expect(mockedUpdateSourceStatus).toHaveBeenCalledWith({
        token: "test-token",
        sourceId: existingSource.id,
        status: "paused",
      });
    });

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Unable to pause source.",
    );
    expect(screen.getByText("Active")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Pause" })).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Resume" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByText("Source paused.")).not.toBeInTheDocument();
  });

  it("resumes a paused source and shows success feedback", async () => {
    const pausedSource: Source = {
      ...existingSource,
      status: "paused",
    };

    mockedListSources.mockResolvedValue({
      data: [pausedSource],
      pagination: { next_cursor: null, has_more: false },
    });
    mockedUpdateSourceStatus.mockResolvedValue({
      ...pausedSource,
      status: "active",
    });

    render(<SourcesPanel token="test-token" />);

    await screen.findByText(pausedSource.url);
    fireEvent.click(screen.getByRole("button", { name: "Resume" }));

    await waitFor(() => {
      expect(mockedUpdateSourceStatus).toHaveBeenCalledWith({
        token: "test-token",
        sourceId: pausedSource.id,
        status: "active",
      });
    });

    expect(await screen.findByText("Active")).toBeInTheDocument();
    expect(await screen.findByText("Source resumed.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Pause" })).toBeInTheDocument();
  });

  it("archives a source and shows the unarchive action", async () => {
    mockedListSources.mockResolvedValue({
      data: [existingSource],
      pagination: { next_cursor: null, has_more: false },
    });
    mockedArchiveSource.mockResolvedValue();
    mockedUnarchiveSource.mockResolvedValue(existingSource);

    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);

    render(<SourcesPanel token="test-token" />);

    await screen.findByText(existingSource.url);
    fireEvent.click(screen.getByRole("button", { name: "Archive" }));

    await waitFor(() => {
      expect(mockedArchiveSource).toHaveBeenCalledWith({
        token: "test-token",
        sourceId: existingSource.id,
      });
    });

    expect(confirmSpy).toHaveBeenCalledWith(
      `Archive ${existingSource.url}? Scheduled runs will be disabled.`,
    );
    expect(await screen.findByText("Archived")).toBeInTheDocument();
    expect(await screen.findByText("Source archived.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Unarchive" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run now" })).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: "Unarchive" }));

    await waitFor(() => {
      expect(mockedUnarchiveSource).toHaveBeenCalledWith({
        token: "test-token",
        sourceId: existingSource.id,
      });
    });

    expect(await screen.findByText("Active")).toBeInTheDocument();
    expect(await screen.findByText("Source unarchived.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Pause" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run now" })).toBeEnabled();
  });

  it("shows the three most recent runs for a source", async () => {
    mockedListSources.mockResolvedValue({
      data: [existingSource],
      pagination: { next_cursor: null, has_more: false },
    });
    mockedListRuns.mockResolvedValue({
      data: [
        completedRun,
        {
          ...completedRun,
          id: "run-2",
          status: "failed",
          triggered_by: "schedule",
          succeeded_tasks: 0,
          failed_tasks: 1,
        },
      ],
      pagination: { next_cursor: null, has_more: false },
    });

    render(<SourcesPanel token="test-token" />);

    expect(await screen.findByText("Recent runs")).toBeInTheDocument();
    expect(screen.getByText("Completed · Manual · 1/1 succeeded")).toBeInTheDocument();
    expect(screen.getByText("Failed · Scheduled · 0/1 succeeded")).toBeInTheDocument();
    expect(mockedListRuns).toHaveBeenCalledWith({
      token: "test-token",
      sourceId: existingSource.id,
      limit: 3,
    });
  });

  it("adds a source and clears the URL field", async () => {
    mockedListSources.mockResolvedValue({
      data: [existingSource],
      pagination: { next_cursor: null, has_more: false },
    });
    mockedCreateSource.mockResolvedValue(addedSource);

    render(<SourcesPanel token="test-token" />);

    await screen.findByText(existingSource.url);

    const urlInput = screen.getByLabelText("Product URL");
    fireEvent.change(urlInput, {
      target: { value: `  ${addedSource.url}  ` },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add source" }));

    await waitFor(() => {
      expect(mockedCreateSource).toHaveBeenCalledWith({
        token: "test-token",
        url: addedSource.url,
      });
    });

    expect(await screen.findByText(addedSource.url)).toBeInTheDocument();
    expect(await screen.findByText("Source added.")).toBeInTheDocument();
    expect(urlInput).toHaveValue("");
  });

  it("keeps the URL and shows an error when adding a source fails", async () => {
    mockedListSources.mockResolvedValue({
      data: [existingSource],
      pagination: { next_cursor: null, has_more: false },
    });
    mockedCreateSource.mockRejectedValue(
      new ApiError("This product URL is not supported.", 422),
    );

    render(<SourcesPanel token="test-token" />);

    await screen.findByText(existingSource.url);

    const urlInput = screen.getByLabelText("Product URL");
    fireEvent.change(urlInput, {
      target: { value: addedSource.url },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add source" }));

    await waitFor(() => {
      expect(mockedCreateSource).toHaveBeenCalledWith({
        token: "test-token",
        url: addedSource.url,
      });
    });

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "This product URL is not supported.",
    );
    expect(urlInput).toHaveValue(addedSource.url);
    expect(screen.queryByText("Source added.")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: addedSource.url }),
    ).not.toBeInTheDocument();
  });

  it("triggers a run and shows its latest completed status", async () => {
    mockedListSources.mockResolvedValue({
      data: [existingSource],
      pagination: { next_cursor: null, has_more: false },
    });
    mockedTriggerSourceRun.mockResolvedValue({
      run_id: completedRun.id,
      task_id: "task-1",
      status: "pending",
    });
    mockedGetRun.mockResolvedValue(completedRun);

    render(<SourcesPanel token="test-token" />);

    await screen.findByText(existingSource.url);
    fireEvent.click(screen.getByRole("button", { name: "Run now" }));

    await waitFor(() => {
      expect(mockedTriggerSourceRun).toHaveBeenCalledWith({
        token: "test-token",
        sourceId: existingSource.id,
      });
    });

    expect(await screen.findByText(/Latest run: Completed/)).toBeInTheDocument();
    expect(screen.getByText(/1\/1 tasks succeeded/)).toBeInTheDocument();
    expect(mockedGetRun).toHaveBeenCalledWith({
      token: "test-token",
      runId: completedRun.id,
    });
  });

  it("polls an in-progress run until it completes", async () => {
    vi.useFakeTimers();

    mockedListSources.mockResolvedValue({
      data: [existingSource],
      pagination: { next_cursor: null, has_more: false },
    });
    mockedTriggerSourceRun.mockResolvedValue({
      run_id: runningRun.id,
      task_id: "task-1",
      status: "pending",
    });
    mockedGetRun
      .mockResolvedValueOnce(runningRun)
      .mockResolvedValueOnce(completedRun);

    render(<SourcesPanel token="test-token" />);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });

    expect(screen.getByText(existingSource.url)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Run now" }));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });

    expect(screen.getByText(/Latest run: Running/)).toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });

    expect(screen.getByText(/Latest run: Completed/)).toBeInTheDocument();
    expect(mockedGetRun).toHaveBeenCalledTimes(2);
  });

  it("shows a source-level error when triggering a run fails", async () => {
    mockedListSources.mockResolvedValue({
      data: [existingSource],
      pagination: { next_cursor: null, has_more: false },
    });
    mockedTriggerSourceRun.mockRejectedValue(new ApiError("Run already in progress.", 409));

    render(<SourcesPanel token="test-token" />);

    await screen.findByText(existingSource.url);
    fireEvent.click(screen.getByRole("button", { name: "Run now" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Run already in progress.");
    expect(screen.getByRole("button", { name: "Run now" })).toBeEnabled();
  });

  it("shows an API error when loading sources fails", async () => {
    mockedListSources.mockRejectedValue(new ApiError("Unauthorized", 401));

    render(<SourcesPanel token="invalid-token" />);

    expect(await screen.findByRole("alert")).toHaveTextContent("Unauthorized");
  });
});
