import { useEffect, type ReactNode } from "react";
import { Navigate } from "react-router-dom";
import { useAuth } from "@/store/auth";

export function ProtectedRoute({ children }: { children: ReactNode }) {
  const { user, initialized, loadUser } = useAuth();

  useEffect(() => {
    if (!user && initialized) {
      // No user and already tried loading — redirect to login
    } else if (!user && !initialized) {
      loadUser();
    }
  }, [user, initialized, loadUser]);

  if (!initialized) {
    return (
      <div className="flex h-screen items-center justify-center">
        <div className="h-6 w-6 animate-spin rounded-full border-2 border-primary border-t-transparent" />
      </div>
    );
  }

  if (!user) {
    return <Navigate to="/login" replace />;
  }

  return <>{children}</>;
}
