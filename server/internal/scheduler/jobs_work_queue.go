package scheduler

import (
	"context"
	"time"
)

const JobNameWorkQueueDispatch = "work_queue_dispatch"

// WorkQueueTicker is implemented by service.WorkQueueService; declared here so
// the scheduler package does not import the service package (mirrors
// AutopilotScheduleDispatcher).
type WorkQueueTicker interface {
	TickAll(ctx context.Context, now time.Time) (int, error)
}

// WorkQueueDispatchJob drains runnable work queues on a fixed cadence. All
// per-queue promotion (scheduled/cron) and the sequential-dispatch guarantee
// live in the service; this job is just the periodic heartbeat.
func WorkQueueDispatchJob(ticker WorkQueueTicker) JobSpec {
	return JobSpec{
		Name:              JobNameWorkQueueDispatch,
		Cadence:           30 * time.Second,
		CatchUpMode:       CatchUpLatestOnly,
		RunTimeout:        2 * time.Minute,
		StaleTimeout:      5 * time.Minute,
		HeartbeatInterval: 30 * time.Second,
		AllowStaleReentry: true,
		MaxAttempts:       1,
		Scopes:            StaticScopes(ScopeGlobal),
		Handler: func(ctx context.Context, in HandlerInput) (HandlerResult, error) {
			n, err := ticker.TickAll(ctx, in.PlanTime)
			return HandlerResult{RowsAffected: int64(n)}, err
		},
	}
}
