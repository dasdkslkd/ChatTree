export interface Branch {
  node_id: string;
  message_preview: string;
  depth: number;
  children: Branch[];
}