import { useState } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { ArrowLeft, Check, X } from 'lucide-react';
import { supabase } from '@/integrations/supabase/client';
import { useAuth } from '@/contexts/AuthContext';
import { Card } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';
import { useToast } from '@/hooks/use-toast';

// Shape a Claude-based email parser will write into staged_imports.parsed.
interface ParsedBooking {
  provider: string;
  car_class: string;
  airport_code: string;
  pickup_date: string;
  pickup_time: string;
  return_date: string;
  return_time: string;
  booked_price: number;
  driver_age?: number;
  confirmation_number?: string;
}

export default function ImportReview() {
  const { importId } = useParams();
  const navigate = useNavigate();
  const { user } = useAuth();
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const [busy, setBusy] = useState(false);

  const { data: staged, isLoading } = useQuery({
    enabled: !!importId,
    queryKey: ['staged_import', importId],
    queryFn: async () => {
      const { data, error } = await supabase.from('staged_imports').select('*').eq('id', importId!).single();
      if (error) throw error;
      return data;
    },
  });

  const confirm = useMutation({
    mutationFn: async () => {
      if (!staged || !user) return;
      const parsed = staged.parsed as unknown as ParsedBooking;
      const { data: booking, error: bookingError } = await supabase.from('bookings').insert({
        user_id: user.id,
        provider: parsed.provider,
        car_class: parsed.car_class,
        airport_code: parsed.airport_code,
        pickup_date: parsed.pickup_date,
        pickup_time: parsed.pickup_time,
        return_date: parsed.return_date,
        return_time: parsed.return_time,
        booked_price: parsed.booked_price,
        driver_age: parsed.driver_age ?? 30,
        status: 'active',
      }).select().single();
      if (bookingError) throw bookingError;

      const { error: updateError } = await supabase.from('staged_imports')
        .update({ status: 'confirmed', booking_id: booking.id, reviewed_at: new Date().toISOString() })
        .eq('id', staged.id);
      if (updateError) throw updateError;
      return booking;
    },
    onSuccess: (booking) => {
      toast({ title: 'Booking added', description: 'Now tracking this booking.' });
      queryClient.invalidateQueries({ queryKey: ['bookings'] });
      navigate(booking ? `/bookings/${booking.id}` : '/home');
    },
    onError: (err) => toast({ title: 'Could not confirm', description: (err as Error).message, variant: 'destructive' }),
  });

  const reject = useMutation({
    mutationFn: async () => {
      if (!staged) return;
      const { error } = await supabase.from('staged_imports')
        .update({ status: 'rejected', reviewed_at: new Date().toISOString() })
        .eq('id', staged.id);
      if (error) throw error;
    },
    onSuccess: () => {
      toast({ title: 'Dismissed' });
      navigate('/home');
    },
  });

  if (isLoading) {
    return (
      <div className="p-6 md:p-10 max-w-lg mx-auto space-y-4">
        <Skeleton className="h-8 w-32" />
        <Skeleton className="h-64 w-full rounded-2xl" />
      </div>
    );
  }

  if (!staged) {
    return <div className="p-6 md:p-10 max-w-lg mx-auto"><p className="text-sm text-destructive">Import not found.</p></div>;
  }

  const parsed = staged.parsed as unknown as ParsedBooking;

  return (
    <div className="p-6 md:p-10 max-w-lg mx-auto space-y-6">
      <button onClick={() => navigate(-1)} className="flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground">
        <ArrowLeft className="h-4 w-4" /> Back
      </button>

      <div>
        <h1 className="text-2xl font-bold text-foreground">Confirm this booking?</h1>
        <p className="text-muted-foreground mt-1">Found in your Gmail — check the details before we start tracking it.</p>
      </div>

      <Card className="p-5 space-y-3">
        <Row label="Provider" value={parsed.provider} />
        <Row label="Car class" value={parsed.car_class} />
        <Row label="Location" value={parsed.airport_code} />
        <Row label="Pickup" value={`${parsed.pickup_date} ${parsed.pickup_time}`} />
        <Row label="Return" value={`${parsed.return_date} ${parsed.return_time}`} />
        <Row label="Price" value={`$${parsed.booked_price?.toFixed(2)}`} />
        {parsed.confirmation_number && <Row label="Confirmation #" value={parsed.confirmation_number} />}
      </Card>

      {staged.status !== 'pending' ? (
        <p className="text-sm text-muted-foreground text-center">
          {staged.status === 'confirmed' ? 'Already confirmed.' : 'Already dismissed.'}
        </p>
      ) : (
        <div className="flex gap-3">
          <Button variant="outline" className="flex-1 gap-2" onClick={() => reject.mutate()} disabled={reject.isPending || confirm.isPending}>
            <X className="h-4 w-4" /> Not mine
          </Button>
          <Button className="flex-1 gap-2" onClick={() => confirm.mutate()} disabled={reject.isPending || confirm.isPending}>
            <Check className="h-4 w-4" /> Start tracking
          </Button>
        </div>
      )}
    </div>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between text-sm">
      <span className="text-muted-foreground">{label}</span>
      <span className="font-medium text-foreground">{value}</span>
    </div>
  );
}
