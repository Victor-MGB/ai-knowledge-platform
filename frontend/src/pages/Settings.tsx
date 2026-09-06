import { useEffect, useState } from "react";
import {
  orgApi,
  type OrgProfile,
  type Member,
  type Invitation,
} from "@/lib/api";
import { useAuth } from "@/store/auth";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Select } from "@/components/ui/select";
import { Separator } from "@/components/ui/separator";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { formatDate } from "@/lib/utils";
import {
  RiTeamLine,
  RiMailAddLine,
  RiDeleteBinLine,
  RiUserSettingsLine,
  RiShieldLine,
  RiMailLine,
} from "react-icons/ri";

export default function Settings() {
  const { user } = useAuth();
  const isAdmin = user?.role === "owner" || user?.role === "admin";

  const [org, setOrg] = useState<OrgProfile | null>(null);
  const [members, setMembers] = useState<Member[]>([]);
  const [invitations, setInvitations] = useState<Invitation[]>([]);
  const [inviteEmail, setInviteEmail] = useState("");
  const [inviteRole, setInviteRole] = useState("member");
  const [inviteDialog, setInviteDialog] = useState(false);
  const [inviteToken, setInviteToken] = useState("");

  useEffect(() => {
    orgApi.get().then(setOrg);
    orgApi.members().then((r) => setMembers(r.items));
    if (isAdmin) {
      orgApi.invitations().then((r) => setInvitations(r.items)).catch(() => {});
    }
  }, [isAdmin]);

  const doInvite = async () => {
    if (!inviteEmail.trim()) return;
    try {
      const inv = await orgApi.invite(inviteEmail, inviteRole);
      setInvitations((prev) => [inv, ...prev]);
      setInviteToken(inv.token ?? "");
      setInviteEmail("");
    } catch {
      // handled by backend error
    }
  };

  const doRevoke = async (id: string) => {
    await orgApi.revokeInvitation(id);
    setInvitations((prev) => prev.filter((i) => i.id !== id));
  };

  const doRoleChange = async (memberId: string, newRole: string) => {
    await orgApi.updateRole(memberId, newRole);
    setMembers((prev) =>
      prev.map((m) => (m.id === memberId ? { ...m, role: newRole } : m)),
    );
  };

  return (
    <div className="p-6 max-w-4xl mx-auto space-y-6">
      <h1 className="text-2xl font-bold">Settings</h1>

      {/* Org profile */}
      {org && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base flex items-center gap-2">
              <RiTeamLine size={18} /> Organization
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-3 text-sm">
            <div className="flex justify-between">
              <span className="text-muted-foreground">Name</span>
              <span className="font-medium">{org.name}</span>
            </div>
            <Separator />
            <div className="flex justify-between">
              <span className="text-muted-foreground">Slug</span>
              <Badge variant="outline">{org.slug}</Badge>
            </div>
            <Separator />
            <div className="flex justify-between">
              <span className="text-muted-foreground">Members</span>
              <span>{org.memberCount}</span>
            </div>
          </CardContent>
        </Card>
      )}

      {/* Members */}
      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <CardTitle className="text-base flex items-center gap-2">
            <RiShieldLine size={18} /> Members
          </CardTitle>
          {isAdmin && (
            <Button size="sm" onClick={() => setInviteDialog(true)}>
              <RiMailAddLine size={14} /> Invite
            </Button>
          )}
        </CardHeader>
        <CardContent>
          {members.length === 0 ? (
            <p className="text-sm text-muted-foreground">Loading members...</p>
          ) : (
            <div className="space-y-2">
              {members.map((m) => (
                <div
                  key={m.id}
                  className="flex items-center justify-between rounded-md border p-3"
                >
                  <div className="flex items-center gap-3 min-w-0">
                    <div className="h-8 w-8 rounded-full bg-muted flex items-center justify-center text-xs font-medium shrink-0">
                      {m.email.slice(0, 2).toUpperCase()}
                    </div>
                    <div className="min-w-0">
                      <p className="text-sm font-medium truncate">{m.email}</p>
                      <p className="text-xs text-muted-foreground">
                        Joined {formatDate(m.createdAt)}
                      </p>
                    </div>
                  </div>
                  <div className="flex items-center gap-2 shrink-0">
                    {isAdmin && m.role !== "owner" ? (
                      <Select
                        value={m.role}
                        onChange={(e) => doRoleChange(m.id, e.target.value)}
                        className="w-28 h-8 text-xs"
                      >
                        <option value="admin">Admin</option>
                        <option value="member">Member</option>
                        <option value="viewer">Viewer</option>
                      </Select>
                    ) : (
                      <Badge variant="secondary" className="capitalize">
                        {m.role}
                      </Badge>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      {/* Invitations */}
      {isAdmin && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base flex items-center gap-2">
              <RiMailLine size={18} /> Pending Invitations
            </CardTitle>
          </CardHeader>
          <CardContent>
            {invitations.length === 0 ? (
              <p className="text-sm text-muted-foreground">No pending invitations</p>
            ) : (
              <div className="space-y-2">
                {invitations.map((inv) => (
                  <div
                    key={inv.id}
                    className="flex items-center justify-between rounded-md border p-3"
                  >
                    <div className="min-w-0">
                      <p className="text-sm font-medium">{inv.email}</p>
                      <p className="text-xs text-muted-foreground">
                        {inv.status} · expires {formatDate(inv.expiresAt)}
                      </p>
                    </div>
                    <div className="flex items-center gap-2 shrink-0">
                      <Badge variant="outline" className="capitalize">
                        {inv.role}
                      </Badge>
                      {inv.status === "pending" && (
                        <button
                          onClick={() => doRevoke(inv.id)}
                          className="p-1.5 rounded text-muted-foreground hover:text-destructive hover:bg-destructive/10"
                        >
                          <RiDeleteBinLine size={14} />
                        </button>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </CardContent>
        </Card>
      )}

      {/* Invite dialog */}
      <Dialog open={inviteDialog} onOpenChange={setInviteDialog}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Invite a team member</DialogTitle>
          </DialogHeader>
          <div className="space-y-4 mt-4">
            <Input
              type="email"
              placeholder="Email address"
              value={inviteEmail}
              onChange={(e) => setInviteEmail(e.target.value)}
            />
            <Select value={inviteRole} onChange={(e) => setInviteRole(e.target.value)}>
              <option value="member">Member</option>
              <option value="admin">Admin</option>
              <option value="viewer">Viewer</option>
            </Select>
            <Button onClick={doInvite} className="w-full">
              Send invitation
            </Button>
            {inviteToken && (
              <div className="rounded-md bg-muted p-3 text-xs">
                <p className="font-medium mb-1">Invitation link:</p>
                <p className="break-all text-muted-foreground">
                  Token: {inviteToken}
                </p>
              </div>
            )}
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
