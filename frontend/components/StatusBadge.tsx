import type { Status } from "@/lib/api";

const LABEL: Record<Status, string> = {
  draft: "Draft",
  approved: "Approved",
  queued: "Queued",
  sending: "Sending",
  sent: "Sent",
};

export default function StatusBadge({ status }: { status: Status }) {
  return <span className={`badge ${status}`}>{LABEL[status] ?? status}</span>;
}
