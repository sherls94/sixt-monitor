import { Outlet, useNavigate } from 'react-router-dom';
import { useAuth } from '@/contexts/AuthContext';
import { Car, Search, Settings as SettingsIcon, LogOut } from 'lucide-react';
import { NavLink } from '@/components/NavLink';
import { Button } from '@/components/ui/button';

const navItems = [
  { to: '/home', label: 'Home', icon: Car },
  { to: '/search', label: 'Search', icon: Search },
  { to: '/settings', label: 'Settings', icon: SettingsIcon },
];

export default function AppLayout() {
  const { signOut } = useAuth();
  const navigate = useNavigate();

  const handleSignOut = async () => {
    await signOut();
    navigate('/login');
  };

  return (
    <div className="min-h-screen flex flex-col md:flex-row bg-background">
      {/* Desktop sidebar */}
      <aside className="hidden md:flex md:w-[260px] md:flex-col md:border-r border-border/60 bg-card">
        <div className="flex items-center gap-2.5 px-6 py-6 border-b border-border/60">
          <div className="flex h-8 w-8 items-center justify-center rounded-xl bg-foreground">
            <Car className="h-4 w-4 text-background" />
          </div>
          <span className="text-lg font-bold tracking-editorial text-foreground">Rentals</span>
        </div>
        <nav className="flex-1 flex flex-col gap-0.5 p-4">
          {navItems.map(item => (
            <NavLink
              key={item.to}
              to={item.to}
              className="flex items-center gap-3 rounded-xl px-4 py-2.5 text-sm font-medium text-muted-foreground transition-all duration-150 hover:bg-secondary hover:text-foreground"
              activeClassName="bg-secondary text-foreground font-semibold"
            >
              <item.icon className="h-[18px] w-[18px]" strokeWidth={1.75} />
              <span>{item.label}</span>
            </NavLink>
          ))}
        </nav>
        <div className="p-4 border-t border-border/60">
          <Button variant="ghost" className="w-full justify-start gap-3 text-muted-foreground hover:text-foreground" onClick={handleSignOut}>
            <LogOut className="h-[18px] w-[18px]" strokeWidth={1.75} />
            Sign out
          </Button>
        </div>
      </aside>

      {/* Main content */}
      <main className="flex-1 pb-20 md:pb-0">
        <Outlet />
      </main>

      {/* Mobile bottom nav */}
      <nav className="md:hidden fixed bottom-0 inset-x-0 glass border-t border-border/40 flex justify-around py-2.5 z-50" style={{ paddingBottom: 'max(0.625rem, env(safe-area-inset-bottom))' }}>
        {navItems.map(item => (
          <NavLink
            key={item.to}
            to={item.to}
            className="flex flex-col items-center gap-0.5 px-3 py-1 text-[11px] font-medium text-muted-foreground transition-all duration-150"
            activeClassName="text-foreground font-semibold"
          >
            <item.icon className="h-5 w-5" strokeWidth={1.75} />
            <span>{item.label}</span>
          </NavLink>
        ))}
      </nav>
    </div>
  );
}
