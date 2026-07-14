"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import type { Issue } from "@multica/core/types";
import { api } from "@multica/core/api";
import { Check } from "lucide-react";
import {
  Command,
  CommandDialog,
  CommandInput,
  CommandList,
  CommandEmpty,
  CommandGroup,
  CommandItem,
} from "@multica/ui/components/ui/command";
import { Button } from "@multica/ui/components/ui/button";
import { StatusIcon } from "../issues/components/status-icon";
import { useT } from "../i18n";

interface IssuePickerModalProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description: string;
  excludeIds: string[];
  onSelect: (issue: Issue) => void;
  /** Load recent issues on open instead of waiting for a search query. */
  preloadAll?: boolean;
  /** Toggle-select many issues; confirm via footer button -> onSelectMany. */
  multiple?: boolean;
  onSelectMany?: (issues: Issue[]) => void;
}

export function IssuePickerModal({
  open,
  onOpenChange,
  title,
  description,
  excludeIds,
  onSelect,
  preloadAll = false,
  multiple = false,
  onSelectMany,
}: IssuePickerModalProps) {
  const { t } = useT("modals");
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<Issue[]>([]);
  const [selected, setSelected] = useState<Issue[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const debounceRef = useRef<ReturnType<typeof setTimeout>>(undefined);
  const abortRef = useRef<AbortController>(undefined);
  // Monotonic token so a stale preload response never clobbers newer results
  // (api.listIssues takes no abort signal).
  const preloadSeqRef = useRef(0);

  useEffect(() => {
    if (!open) {
      setQuery("");
      setResults([]);
      setSelected([]);
      setIsLoading(false);
    }
  }, [open]);

  const preload = useCallback(async () => {
    const seq = ++preloadSeqRef.current;
    setIsLoading(true);
    try {
      const res = await api.listIssues({ limit: 50 });
      if (preloadSeqRef.current === seq) {
        setResults(res.issues.filter((i) => !excludeIds.includes(i.id)));
        setIsLoading(false);
      }
    } catch {
      if (preloadSeqRef.current === seq) {
        setIsLoading(false);
      }
    }
  }, [excludeIds]);

  useEffect(() => {
    if (open && preloadAll) {
      void preload();
    }
  }, [open, preloadAll, preload]);

  const search = useCallback(
    (q: string) => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
      if (abortRef.current) abortRef.current.abort();
      // Invalidate any in-flight preload so it can't overwrite search results.
      preloadSeqRef.current++;

      if (!q.trim()) {
        if (preloadAll) {
          void preload();
        } else {
          setResults([]);
          setIsLoading(false);
        }
        return;
      }

      setIsLoading(true);
      debounceRef.current = setTimeout(async () => {
        const controller = new AbortController();
        abortRef.current = controller;
        try {
          const res = await api.searchIssues({
            q: q.trim(),
            limit: 20,
            include_closed: true,
            signal: controller.signal,
          });
          if (!controller.signal.aborted) {
            setResults(res.issues.filter((i) => !excludeIds.includes(i.id)));
            setIsLoading(false);
          }
        } catch {
          if (!controller.signal.aborted) {
            setIsLoading(false);
          }
        }
      }, 300);
    },
    [excludeIds, preloadAll, preload],
  );

  const toggleSelected = (issue: Issue) => {
    setSelected((prev) =>
      prev.some((i) => i.id === issue.id)
        ? prev.filter((i) => i.id !== issue.id)
        : [...prev, issue],
    );
  };

  return (
    <CommandDialog
      open={open}
      onOpenChange={onOpenChange}
      title={title}
      description={description}
    >
      <Command shouldFilter={false}>
        <CommandInput
          placeholder={t(($) => $.issue_picker.search_placeholder)}
          value={query}
          onValueChange={(v) => {
            setQuery(v);
            search(v);
          }}
        />
        <CommandList>
          {isLoading && (
            <div className="py-6 text-center text-sm text-muted-foreground">
              {t(($) => $.issue_picker.searching)}
            </div>
          )}
          {!isLoading && (query.trim() || preloadAll) && results.length === 0 && (
            <CommandEmpty>{t(($) => $.issue_picker.no_results)}</CommandEmpty>
          )}
          {!isLoading && !query.trim() && !preloadAll && (
            <div className="py-6 text-center text-sm text-muted-foreground">
              {t(($) => $.issue_picker.prompt_to_search)}
            </div>
          )}
          {results.length > 0 && (
            <CommandGroup>
              {results.map((issue) => (
                <CommandItem
                  key={issue.id}
                  value={issue.id}
                  onSelect={() => {
                    if (multiple) {
                      toggleSelected(issue);
                    } else {
                      onSelect(issue);
                      onOpenChange(false);
                    }
                  }}
                >
                  <StatusIcon status={issue.status} className="h-3.5 w-3.5 shrink-0" />
                  <span className="text-muted-foreground shrink-0">{issue.identifier}</span>
                  <span className="truncate">{issue.title}</span>
                  {multiple && selected.some((i) => i.id === issue.id) && (
                    <Check className="ml-auto size-3.5 shrink-0" />
                  )}
                </CommandItem>
              ))}
            </CommandGroup>
          )}
        </CommandList>
        {multiple && (
          <div className="border-t p-2">
            <Button
              size="sm"
              className="w-full"
              disabled={selected.length === 0}
              onClick={() => {
                onSelectMany?.(selected);
                onOpenChange(false);
              }}
            >
              {t(($) => $.issue_picker.add_selected, { count: selected.length })}
            </Button>
          </div>
        )}
      </Command>
    </CommandDialog>
  );
}
