"use client";

import { use } from "react";
import { QueueDetailPage } from "@multica/views/queues/components";

export default function Page({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  return <QueueDetailPage queueId={id} />;
}
