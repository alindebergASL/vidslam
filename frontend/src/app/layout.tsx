import "./globals.css";
import type { Metadata } from "next";
import { SideNav } from "@/components/SideNav";

export const metadata: Metadata = {
  title: "AvatarVideoStudio",
  description: "Create short-form video from reusable digital avatars.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen bg-ink-950">
        <div className="flex min-h-screen">
          <SideNav />
          <main className="flex-1 min-w-0">{children}</main>
        </div>
      </body>
    </html>
  );
}
