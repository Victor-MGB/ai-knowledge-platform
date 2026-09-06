import { type ReactNode, createContext, useContext, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "@/store/auth";
import { cn } from "@/lib/utils";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { Separator } from "@/components/ui/separator";
import {
  RiDashboardLine,
  RiFileTextLine,
  RiChat3Line,
  RiSettings3Line,
  RiLogoutBoxLine,
  RiMenuLine,
  RiCloseLine,
  RiTeamLine,
} from "react-icons/ri";

const SidebarCtx = createContext({ collapsed: false, toggle: () => {} });

const navItems = [
  { to: "/", icon: RiDashboardLine, label: "Dashboard" },
  { to: "/documents", icon: RiFileTextLine, label: "Documents" },
  { to: "/chat", icon: RiChat3Line, label: "Chat" },
];

const navBottom = [
  { to: "/settings", icon: RiSettings3Line, label: "Settings" },
];

function NavLink({
  to,
  icon: Icon,
  label,
}: {
  to: string;
  icon: React.ComponentType<{ size?: number }>;
  label: string;
}) {
  const loc = useLocation();
  const { collapsed } = useContext(SidebarCtx);
  const active =
    to === "/" ? loc.pathname === "/" : loc.pathname.startsWith(to);

  return (
    <Link
      to={to}
      className={cn(
        "flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors",
        active
          ? "bg-primary text-primary-foreground"
          : "text-muted-foreground hover:bg-accent hover:text-accent-foreground",
        collapsed && "justify-center px-2",
      )}
      title={collapsed ? label : undefined}
    >
      <Icon size={18} />
      {!collapsed && <span>{label}</span>}
    </Link>
  );
}

export function Layout({ children }: { children: ReactNode }) {
  const [collapsed, setCollapsed] = useState(false);
  const { user, logout } = useAuth();
  const nav = useNavigate();

  const initials = user?.email?.slice(0, 2).toUpperCase() ?? "??";

  return (
    <SidebarCtx.Provider value={{ collapsed, toggle: () => setCollapsed((c) => !c) }}>
      <div className="flex h-screen overflow-hidden">
        {/* Sidebar */}
        <aside
          className={cn(
            "flex flex-col border-r bg-sidebar transition-all duration-200",
            collapsed ? "w-16" : "w-60",
          )}
        >
          {/* Header */}
          <div
            className={cn(
              "flex h-14 items-center border-b px-4",
              collapsed && "justify-center px-2",
            )}
          >
            {!collapsed && (
              <span className="text-base font-bold tracking-tight">KnowFlow</span>
            )}
            <button
              onClick={() => setCollapsed((c) => !c)}
              className={cn(
                "rounded p-1 text-muted-foreground hover:bg-accent hover:text-accent-foreground",
                collapsed ? "mt-0" : "ml-auto",
              )}
            >
              {collapsed ? <RiMenuLine size={18} /> : <RiCloseLine size={18} />}
            </button>
          </div>

          {/* Nav */}
          <nav className="flex-1 space-y-1 p-2">
            {navItems.map((item) => (
              <NavLink key={item.to} {...item} />
            ))}
            {user?.role === "owner" || user?.role === "admin" ? (
              <NavLink to="/settings" icon={RiTeamLine} label="Team" />
            ) : null}
          </nav>

          {/* Bottom */}
          <div className="space-y-1 p-2">
            <Separator className="mb-2" />
            {navBottom.map((item) => (
              <NavLink key={item.to} {...item} />
            ))}
            <button
              onClick={async () => {
                await logout();
                nav("/login");
              }}
              className={cn(
                "flex w-full items-center gap-3 rounded-md px-3 py-2 text-sm font-medium text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground",
                collapsed && "justify-center px-2",
              )}
              title={collapsed ? "Sign out" : undefined}
            >
              <RiLogoutBoxLine size={18} />
              {!collapsed && <span>Sign out</span>}
            </button>
          </div>

          {/* User */}
          <div
            className={cn(
              "flex items-center gap-3 border-t p-3",
              collapsed && "justify-center",
            )}
          >
            <Avatar className="h-8 w-8">
              <AvatarFallback>{initials}</AvatarFallback>
            </Avatar>
            {!collapsed && (
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm font-medium">{user?.email}</p>
                <p className="truncate text-xs text-muted-foreground capitalize">
                  {user?.role}
                </p>
              </div>
            )}
          </div>
        </aside>

        {/* Main */}
        <main className="flex-1 overflow-auto bg-background">{children}</main>
      </div>
    </SidebarCtx.Provider>
  );
}
