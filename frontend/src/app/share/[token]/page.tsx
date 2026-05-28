import type { Metadata } from "next";
import { SharePlayer } from "./SharePlayer";

// Server-side base for metadata fetch (inside the container the public base may
// differ from the browser-facing one). Falls back to the public base.
const PUBLIC_API_BASE = process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8000";
const INTERNAL_API_BASE = process.env.INTERNAL_API_BASE || PUBLIC_API_BASE;

type Props = { params: { token: string } };

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { token } = params;
  const fallback: Metadata = {
    title: "Shared video · AvatarVideoStudio",
    description: "AI-generated virtual creator content.",
  };
  try {
    const r = await fetch(`${INTERNAL_API_BASE}/api/public-renders/${token}/meta`, {
      cache: "no-store",
    });
    if (!r.ok) return fallback;
    const meta = (await r.json()) as { title: string; disclosure: string };
    const thumb = `${PUBLIC_API_BASE}/api/public-renders/${token}/thumbnail`;
    const videoUrl = `${PUBLIC_API_BASE}/api/public-renders/${token}`;
    const title = `${meta.title} · AvatarVideoStudio`;
    const description = meta.disclosure || "AI-generated virtual creator content.";
    return {
      title,
      description,
      openGraph: {
        title,
        description,
        type: "video.other",
        images: [{ url: thumb, width: 1080, height: 1920 }],
        videos: [{ url: videoUrl, type: "video/mp4", width: 1080, height: 1920 }],
      },
      twitter: {
        card: "summary_large_image",
        title,
        description,
        images: [thumb],
      },
    };
  } catch {
    return fallback;
  }
}

export default function SharePage({ params }: Props) {
  return <SharePlayer token={params.token} />;
}
