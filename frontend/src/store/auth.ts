import { create } from "zustand";
import {
  authApi,
  meApi,
  clearTokens,
  setTokens,
  setOnUnauthorized,
  type User,
} from "@/lib/api";

interface AuthState {
  user: User | null;
  loading: boolean;
  initialized: boolean;
  login: (email: string, password: string) => Promise<void>;
  register: (
    email: string,
    password: string,
    orgName?: string,
    name?: string,
  ) => Promise<void>;
  logout: () => Promise<void>;
  loadUser: () => Promise<void>;
  setUser: (u: User | null) => void;
}

export const useAuth = create<AuthState>((set, get) => ({
  user: null,
  loading: false,
  initialized: false,

  login: async (email, password) => {
    set({ loading: true });
    try {
      const { user, tokens } = await authApi.login({ email, password });
      setTokens(tokens.accessToken, tokens.refreshToken);
      set({ user, loading: false });
    } catch (e) {
      set({ loading: false });
      throw e;
    }
  },

  register: async (email, password, orgName, name) => {
    set({ loading: true });
    try {
      const { user, tokens } = await authApi.register({
        email,
        password,
        organization: orgName,
        name,
      });
      setTokens(tokens.accessToken, tokens.refreshToken);
      set({ user, loading: false });
    } catch (e) {
      set({ loading: false });
      throw e;
    }
  },

  logout: async () => {
    try {
      await authApi.logout();
    } catch {
      // ignore
    }
    clearTokens();
    set({ user: null });
  },

  loadUser: async () => {
    try {
      const user = await meApi.get();
      set({ user, initialized: true });
    } catch {
      set({ user: null, initialized: true });
    }
  },

  setUser: (u) => set({ user: u }),
}));

setOnUnauthorized(() => {
  useAuth.getState().setUser(null);
});
