package issueguard

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"
	"github.com/multica-ai/multica/server/internal/util"
	db "github.com/multica-ai/multica/server/pkg/db/generated"
)

func NormalizeTitle(title string) string {
	return strings.ToLower(strings.Join(strings.Fields(title), " "))
}

func DuplicateMessage(identifier, title, status string) string {
	return "Active duplicate issue exists: " + identifier + " " + title + " (status: " + status + "). Set allow_duplicate=true or use --allow-duplicate to create another."
}

type ActiveDuplicateError struct {
	ID         string
	Identifier string
	Title      string
	Status     string
}

func (e *ActiveDuplicateError) Error() string {
	return DuplicateMessage(e.Identifier, e.Title, e.Status)
}

func NewActiveDuplicateError(issue db.Issue, issuePrefix string) *ActiveDuplicateError {
	return &ActiveDuplicateError{
		ID:         util.UUIDToString(issue.ID),
		Identifier: fmt.Sprintf("%s-%d", issuePrefix, issue.Number),
		Title:      issue.Title,
		Status:     issue.Status,
	}
}

func LockAndFindActiveDuplicate(
	ctx context.Context,
	q *db.Queries,
	workspaceID pgtype.UUID,
	projectID pgtype.UUID,
	parentIssueID pgtype.UUID,
	title string,
	allowDuplicate bool,
) (db.Issue, bool, error) {
	normalizedTitle := NormalizeTitle(title)
	if normalizedTitle == "" {
		return db.Issue{}, false, nil
	}
	if err := q.LockIssueDuplicateKey(ctx, lockKey(workspaceID, projectID, parentIssueID, normalizedTitle)); err != nil {
		return db.Issue{}, false, err
	}
	if allowDuplicate {
		return db.Issue{}, false, nil
	}

	duplicate, err := q.FindActiveDuplicateIssue(ctx, db.FindActiveDuplicateIssueParams{
		WorkspaceID:     workspaceID,
		ProjectID:       projectID,
		ParentIssueID:   parentIssueID,
		NormalizedTitle: normalizedTitle,
	})
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return db.Issue{}, false, nil
		}
		return db.Issue{}, false, err
	}
	return duplicate, true, nil
}

func LockAndFindRecentAutopilotDuplicate(
	ctx context.Context,
	q *db.Queries,
	workspaceID pgtype.UUID,
	autopilotID pgtype.UUID,
	projectID pgtype.UUID,
	title string,
	window time.Duration,
) (db.Issue, bool, error) {
	normalizedTitle := NormalizeTitle(title)
	if normalizedTitle == "" || !autopilotID.Valid || window <= 0 {
		return db.Issue{}, false, nil
	}
	if err := q.LockIssueDuplicateKey(ctx, recentAutopilotLockKey(workspaceID, autopilotID, projectID, normalizedTitle)); err != nil {
		return db.Issue{}, false, err
	}

	duplicate, err := q.FindRecentAutopilotDuplicateIssue(ctx, db.FindRecentAutopilotDuplicateIssueParams{
		WorkspaceID:     workspaceID,
		OriginID:        autopilotID,
		ProjectID:       projectID,
		NormalizedTitle: normalizedTitle,
		CreatedAfter:    pgtype.Timestamptz{Time: time.Now().UTC().Add(-window), Valid: true},
	})
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return db.Issue{}, false, nil
		}
		return db.Issue{}, false, err
	}
	return duplicate, true, nil
}

// LockAndFindWorkQueueItemIssue guards work-queue prompt dispatch against
// re-creating an issue after a crash between tx.Commit (issue created) and
// MarkWorkQueueItemRunning (item marked running + linked): on retry the item
// is still pending, but the issue already exists. Unlike the autopilot
// duplicate finders, origin_id here is a work_queue_item.id -- already a
// unique per-attempt key -- so this is an exact lookup, not a title/window
// match.
func LockAndFindWorkQueueItemIssue(
	ctx context.Context,
	q *db.Queries,
	itemID pgtype.UUID,
) (db.Issue, bool, error) {
	if err := q.LockIssueDuplicateKey(ctx, workQueueItemLockKey(itemID)); err != nil {
		return db.Issue{}, false, err
	}

	issue, err := q.FindWorkQueueItemIssue(ctx, itemID)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return db.Issue{}, false, nil
		}
		return db.Issue{}, false, err
	}
	return issue, true, nil
}

func workQueueItemLockKey(itemID pgtype.UUID) string {
	return "work-queue-item-issue|" + util.UUIDToString(itemID)
}

func lockKey(workspaceID, projectID, parentIssueID pgtype.UUID, normalizedTitle string) string {
	return strings.Join([]string{
		"issue-active-duplicate",
		util.UUIDToString(workspaceID),
		util.UUIDToString(projectID),
		util.UUIDToString(parentIssueID),
		normalizedTitle,
	}, "|")
}

func recentAutopilotLockKey(workspaceID, autopilotID, projectID pgtype.UUID, normalizedTitle string) string {
	return strings.Join([]string{
		"autopilot-recent-duplicate",
		util.UUIDToString(workspaceID),
		util.UUIDToString(autopilotID),
		util.UUIDToString(projectID),
		normalizedTitle,
	}, "|")
}
