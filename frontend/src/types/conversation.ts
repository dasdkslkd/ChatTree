export interface Conversation {
  id: string;
  title: string;
  created_at: number;
  updated_at: number;
  model: string;
  current_node_id: string;
  total_tokens: {
    prompt_tokens: number;
    completion_tokens: number;
    total_tokens: number;
  };
}

export interface ConversationCreateRequest {
  title?: string;
  prompt_id?: string;
}