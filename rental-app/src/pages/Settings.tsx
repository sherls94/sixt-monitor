import { useNavigate } from 'react-router-dom';
import { Mail, Bell, LogOut } from 'lucide-react';
import { useAuth } from '@/contexts/AuthContext';
import { Card } from '@/components/ui/card';
import { Button } from '@/components/ui/button';

export default function Settings() {
  const { user, signOut } = useAuth();
  const navigate = useNavigate();

  const handleSignOut = async () => {
    await signOut();
    navigate('/login');
  };

  return (
    <div className="p-6 md:p-10 max-w-2xl mx-auto space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-foreground">Settings</h1>
        <p className="text-muted-foreground mt-1">{user?.email}</p>
      </div>

      <Card className="p-5 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-accent">
            <Mail className="h-5 w-5 text-primary" />
          </div>
          <div>
            <div className="font-medium text-foreground">Gmail</div>
            <div className="text-xs text-muted-foreground">Auto-detect booking confirmations</div>
          </div>
        </div>
        <Button variant="outline" size="sm" disabled>
          Coming soon
        </Button>
      </Card>

      <Card className="p-5 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-accent">
            <Bell className="h-5 w-5 text-primary" />
          </div>
          <div>
            <div className="font-medium text-foreground">Notifications</div>
            <div className="text-xs text-muted-foreground">Price drops & import confirmations</div>
          </div>
        </div>
        <Button variant="outline" size="sm" disabled>
          Coming soon
        </Button>
      </Card>

      <Button variant="ghost" className="w-full justify-start gap-3 text-muted-foreground hover:text-foreground" onClick={handleSignOut}>
        <LogOut className="h-[18px] w-[18px]" strokeWidth={1.75} />
        Sign out
      </Button>
    </div>
  );
}
