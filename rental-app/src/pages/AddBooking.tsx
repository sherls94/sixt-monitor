import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ArrowLeft } from 'lucide-react';
import { supabase } from '@/integrations/supabase/client';
import { useAuth } from '@/contexts/AuthContext';
import { triggerBookingCheck } from '@/lib/api';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Card } from '@/components/ui/card';
import { useToast } from '@/hooks/use-toast';

const PROVIDERS = ['SIXT', 'Hertz', 'Enterprise', 'National', 'Alamo', 'Dollar', 'Thrifty'];

export default function AddBooking() {
  const navigate = useNavigate();
  const { user, session } = useAuth();
  const { toast } = useToast();
  const [saving, setSaving] = useState(false);
  const [form, setForm] = useState({
    provider: 'SIXT',
    car_class: 'Full Size SUV',
    airport_code: '',
    pickup_date: '',
    pickup_time: '12:00',
    return_date: '',
    return_time: '12:00',
    booked_price: '',
    driver_age: '30',
  });

  const set = (key: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setForm(f => ({ ...f, [key]: e.target.value }));

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!user) return;
    setSaving(true);
    const { data: booking, error } = await supabase.from('bookings').insert({
      user_id: user.id,
      provider: form.provider,
      car_class: form.car_class,
      airport_code: form.airport_code.trim().toUpperCase(),
      pickup_date: form.pickup_date,
      pickup_time: form.pickup_time,
      return_date: form.return_date,
      return_time: form.return_time,
      booked_price: Number(form.booked_price),
      driver_age: Number(form.driver_age),
      status: 'active',
    }).select().single();
    setSaving(false);
    if (error || !booking) {
      toast({ title: 'Could not save booking', description: error?.message, variant: 'destructive' });
      return;
    }

    // Best-effort: kick off an immediate price check rather than waiting for
    // the next scheduler cycle. A failure here shouldn't block navigation —
    // the booking is already saved and will be picked up on the next cycle.
    if (session?.access_token) {
      triggerBookingCheck(booking.id, session.access_token).catch(() => {});
    }

    toast({ title: 'Booking added', description: 'Checking prices now — check back in a minute.' });
    navigate(`/bookings/${booking.id}`);
  };

  return (
    <div className="p-6 md:p-10 max-w-lg mx-auto space-y-6">
      <button onClick={() => navigate(-1)} className="flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground">
        <ArrowLeft className="h-4 w-4" /> Back
      </button>

      <div>
        <h1 className="text-2xl font-bold text-foreground">Add a booking</h1>
        <p className="text-muted-foreground mt-1">We'll track this and alert you if a better price shows up</p>
      </div>

      <Card className="p-5">
        <form onSubmit={handleSubmit} className="space-y-4">
          <div className="space-y-1.5">
            <Label htmlFor="provider">Booked with</Label>
            <select
              id="provider"
              value={form.provider}
              onChange={e => setForm(f => ({ ...f, provider: e.target.value }))}
              className="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
            >
              {PROVIDERS.map(p => <option key={p} value={p}>{p}</option>)}
            </select>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="car_class">Car class</Label>
            <Input id="car_class" value={form.car_class} onChange={set('car_class')} required />
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="airport_code">Airport code</Label>
            <Input id="airport_code" value={form.airport_code} onChange={set('airport_code')} maxLength={3} placeholder="LGA" required />
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1.5">
              <Label htmlFor="pickup_date">Pickup date</Label>
              <Input id="pickup_date" type="date" value={form.pickup_date} onChange={set('pickup_date')} required />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="pickup_time">Pickup time</Label>
              <Input id="pickup_time" type="time" value={form.pickup_time} onChange={set('pickup_time')} required />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="return_date">Return date</Label>
              <Input id="return_date" type="date" value={form.return_date} onChange={set('return_date')} required />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="return_time">Return time</Label>
              <Input id="return_time" type="time" value={form.return_time} onChange={set('return_time')} required />
            </div>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1.5">
              <Label htmlFor="booked_price">Your price ($)</Label>
              <Input id="booked_price" type="number" step="0.01" min="0" value={form.booked_price} onChange={set('booked_price')} required />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="driver_age">Driver age</Label>
              <Input id="driver_age" type="number" min="18" max="99" value={form.driver_age} onChange={set('driver_age')} required />
            </div>
          </div>

          <Button type="submit" className="w-full" disabled={saving}>
            {saving ? 'Saving…' : 'Start tracking'}
          </Button>
        </form>
      </Card>
    </div>
  );
}
