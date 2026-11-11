import "./globals.css";
import type { Metadata } from "next";
import { SideNav } from "@/components/SideNav";
import { ToastProvider } from "@/components/Toaster";
import { NotificationsProvider } from "@/components/NotificationsProvider";

export const metadata: Metadata = {
  title: "AvatarVideoStudio",
  description: "Create short-form video from reusable digital avatars.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen bg-ink-950">
        <ToastProvider>
          <NotificationsProvider>
            {/* Column on phones (the SideNav renders a sticky top bar there),
                row from md up where it renders the side rail. A plain `flex`
                row made the top bar a horizontal sibling of <main> and
                squeezed every page to ~180px on mobile. */}
            <div className="flex flex-col md:flex-row min-h-screen">
              <SideNav />
              <main className="flex-1 min-w-0">{children}</main>
            </div>
          </NotificationsProvider>
        </ToastProvider>
      </body>
    </html>
  );
}
