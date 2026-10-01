import "./globals.css";
import { AuthProvider } from "../lib/auth";
import { ToastProvider } from "../lib/toast";
import { TelemetryProvider } from "../lib/telemetry/TelemetryProvider";

export const metadata = { title: "LibreRun", description: "An educational software environment for teaching the design, development and operation of AI agents: a self-hosted chassis, built for educational purposes" };

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <AuthProvider>
          <TelemetryProvider />
          <ToastProvider>{children}</ToastProvider>
        </AuthProvider>
      </body>
    </html>
  );
}
