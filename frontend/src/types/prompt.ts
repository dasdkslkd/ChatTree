export interface PromptResponse {
  id: string;
  title: string;
}

export interface Prompt {
  id: string;
  title: string;
  content: string;
}

export interface ListPromptsResponse {
  prompts: PromptResponse[];
}