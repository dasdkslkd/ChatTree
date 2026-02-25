import { useState, useCallback } from 'react';
import { modelApi } from '../api/model';
import type { ModelProvider } from '../types/model';

export const useModels = () => {
  const [providers, setProviders] = useState<ModelProvider[]>([]);
  const [models, setModels] = useState<Record<ModelProvider, string[]>>({} as any);
  const [currentProvider, setCurrentProvider] = useState<ModelProvider | null>(null);
  const [currentModel, setCurrentModel] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<Error | null>(null);

  const loadProviders = useCallback(async () => {
    setLoading(true);
    try {
      const providerList = await modelApi.getProviders();
      setProviders(providerList);
      if (providerList.length > 0) {
        setCurrentProvider(providerList[0]);
      }
    } catch (err) {
      setError(err as Error);
    } finally {
      setLoading(false);
    }
  }, []);

  const loadModels = useCallback(async (provider: ModelProvider) => {
    setLoading(true);
    try {
      const modelList = await modelApi.list(provider);
      setModels((prev) => ({ ...prev, [provider]: modelList }));
    } catch (err) {
      setError(err as Error);
    } finally {
      setLoading(false);
    }
  }, []);

  return {
    providers,
    models,
    currentProvider,
    currentModel,
    loading,
    error,
    loadProviders,
    loadModels,
    setCurrentProvider,
    setCurrentModel,
  };
};