"use client";

import { Button } from "@buildingai/ui/components/ui/button";
import { ButtonGroup, ButtonGroupText } from "@buildingai/ui/components/ui/button-group";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@buildingai/ui/components/ui/tooltip";
import { cn } from "@buildingai/ui/lib/utils";
import { cjk } from "@streamdown/cjk";
import { code } from "@streamdown/code";
import { math } from "@streamdown/math";
import { mermaid } from "@streamdown/mermaid";
import type { UIMessage } from "ai";
import { ChevronLeftIcon, ChevronRightIcon } from "lucide-react";
import type { ComponentProps, HTMLAttributes, ReactElement } from "react";
import { createContext, memo, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { Streamdown } from "streamdown";

export type MessageProps = HTMLAttributes<HTMLDivElement> & {
  from: UIMessage["role"];
};

export const Message = ({ className, from, ...props }: MessageProps) => (
  <div
    className={cn(
      "group flex w-full max-w-[95%] flex-col gap-2",
      from === "user" ? "is-user ml-auto justify-end" : "is-assistant",
      className,
    )}
    {...props}
  />
);

export type MessageContentProps = HTMLAttributes<HTMLDivElement>;

export const MessageContent = ({ children, className, ...props }: MessageContentProps) => (
  <div
    className={cn(
      "is-user:dark flex w-fit max-w-full min-w-0 flex-col gap-2 overflow-hidden text-sm",
      "group-[.is-user]:bg-secondary group-[.is-user]:text-foreground group-[.is-user]:ml-auto group-[.is-user]:rounded-lg group-[.is-user]:px-4 group-[.is-user]:py-3",
      "group-[.is-assistant]:text-foreground",
      className,
    )}
    {...props}
  >
    {children}
  </div>
);

export type MessageActionsProps = ComponentProps<"div">;

export const MessageActions = ({ className, children, ...props }: MessageActionsProps) => (
  <div className={cn("flex items-center gap-1", className)} {...props}>
    {children}
  </div>
);

export type MessageActionProps = ComponentProps<typeof Button> & {
  tooltip?: string;
  label?: string;
};

export const MessageAction = ({
  tooltip,
  children,
  label,
  variant = "ghost",
  size = "icon-sm",
  ...props
}: MessageActionProps) => {
  const button = (
    <Button size={size} type="button" variant={variant} {...props}>
      {children}
      <span className="sr-only">{label || tooltip}</span>
    </Button>
  );

  if (tooltip) {
    return (
      <TooltipProvider>
        <Tooltip>
          <TooltipTrigger asChild>{button}</TooltipTrigger>
          <TooltipContent>
            <p>{tooltip}</p>
          </TooltipContent>
        </Tooltip>
      </TooltipProvider>
    );
  }

  return button;
};

interface MessageBranchContextType {
  currentBranch: number;
  totalBranches: number;
  goToPrevious: () => void;
  goToNext: () => void;
  branches: ReactElement[];
  setBranches: (branches: ReactElement[]) => void;
}

const MessageBranchContext = createContext<MessageBranchContextType | null>(null);

const useMessageBranch = () => {
  const context = useContext(MessageBranchContext);

  if (!context) {
    throw new Error("MessageBranch components must be used within MessageBranch");
  }

  return context;
};

export type MessageBranchProps = HTMLAttributes<HTMLDivElement> & {
  defaultBranch?: number;
  onBranchChange?: (branchIndex: number) => void;
};

export const MessageBranch = ({
  defaultBranch = 0,
  onBranchChange,
  className,
  ...props
}: MessageBranchProps) => {
  const [currentBranch, setCurrentBranch] = useState(defaultBranch);
  const [branches, setBranches] = useState<ReactElement[]>([]);

  const handleBranchChange = useCallback(
    (newBranch: number) => {
      setCurrentBranch(newBranch);
      onBranchChange?.(newBranch);
    },
    [onBranchChange],
  );

  const goToPrevious = useCallback(() => {
    const newBranch = currentBranch > 0 ? currentBranch - 1 : branches.length - 1;
    handleBranchChange(newBranch);
  }, [currentBranch, branches.length, handleBranchChange]);

  const goToNext = useCallback(() => {
    const newBranch = currentBranch < branches.length - 1 ? currentBranch + 1 : 0;
    handleBranchChange(newBranch);
  }, [currentBranch, branches.length, handleBranchChange]);

  const contextValue = useMemo<MessageBranchContextType>(
    () => ({
      branches,
      currentBranch,
      goToNext,
      goToPrevious,
      setBranches,
      totalBranches: branches.length,
    }),
    [branches, currentBranch, goToNext, goToPrevious],
  );

  return (
    <MessageBranchContext.Provider value={contextValue}>
      <div className={cn("grid w-full gap-2 [&>div]:pb-0", className)} {...props} />
    </MessageBranchContext.Provider>
  );
};

export type MessageBranchContentProps = HTMLAttributes<HTMLDivElement>;

export const MessageBranchContent = ({ children, ...props }: MessageBranchContentProps) => {
  const { currentBranch, setBranches, branches } = useMessageBranch();
  const childrenArray = useMemo(
    () => (Array.isArray(children) ? children : [children]),
    [children],
  );

  // Use useEffect to update branches when they change
  useEffect(() => {
    if (branches.length !== childrenArray.length) {
      setBranches(childrenArray);
    }
  }, [childrenArray, branches, setBranches]);

  return childrenArray.map((branch, index) => (
    <div
      className={cn(
        "grid gap-2 overflow-hidden [&>div]:pb-0",
        index === currentBranch ? "block" : "hidden",
      )}
      key={branch.key}
      {...props}
    >
      {branch}
    </div>
  ));
};

export type MessageBranchSelectorProps = ComponentProps<typeof ButtonGroup>;

export const MessageBranchSelector = ({ className, ...props }: MessageBranchSelectorProps) => {
  const { totalBranches } = useMessageBranch();

  // Don't render if there's only one branch
  if (totalBranches <= 1) {
    return null;
  }

  return (
    <ButtonGroup
      className={cn(
        "[&>*:not(:first-child)]:rounded-l-md [&>*:not(:last-child)]:rounded-r-md",
        className,
      )}
      orientation="horizontal"
      {...props}
    />
  );
};

export type MessageBranchPreviousProps = ComponentProps<typeof Button>;

export const MessageBranchPrevious = ({ children, ...props }: MessageBranchPreviousProps) => {
  const { goToPrevious, totalBranches } = useMessageBranch();

  return (
    <Button
      aria-label="Previous branch"
      disabled={totalBranches <= 1}
      onClick={goToPrevious}
      size="icon-sm"
      type="button"
      variant="ghost"
      {...props}
    >
      {children ?? <ChevronLeftIcon size={14} />}
    </Button>
  );
};

export type MessageBranchNextProps = ComponentProps<typeof Button>;

export const MessageBranchNext = ({ children, ...props }: MessageBranchNextProps) => {
  const { goToNext, totalBranches } = useMessageBranch();

  return (
    <Button
      aria-label="Next branch"
      disabled={totalBranches <= 1}
      onClick={goToNext}
      size="icon-sm"
      type="button"
      variant="ghost"
      {...props}
    >
      {children ?? <ChevronRightIcon size={14} />}
    </Button>
  );
};

export type MessageBranchPageProps = HTMLAttributes<HTMLSpanElement>;

export const MessageBranchPage = ({ className, ...props }: MessageBranchPageProps) => {
  const { currentBranch, totalBranches } = useMessageBranch();

  return (
    <ButtonGroupText
      className={cn("text-muted-foreground border-none bg-transparent shadow-none", className)}
      {...props}
    >
      {currentBranch + 1} of {totalBranches}
    </ButtonGroupText>
  );
};

export type MessageResponseProps = ComponentProps<typeof Streamdown>;

const streamdownPlugins = { cjk, code, math, mermaid };

const AUDIO_EXT_RE = /\.(mp3|wav|ogg|m4a|aac|flac|opus|weba)(?:\?.*)?(?:#.*)?$/i;
const VIDEO_EXT_RE = /\.(mp4|webm|ogv|mov|m4v|avi|mkv)(?:\?.*)?(?:#.*)?$/i;

type MediaKind = "video" | "audio" | "link";

/** Classify a URL by its file extension (fast path, no network request). */
function mediaKindFromExtension(url: string): MediaKind | null {
  if (VIDEO_EXT_RE.test(url)) return "video";
  if (AUDIO_EXT_RE.test(url)) return "audio";
  return null;
}

/** Classify a URL by its `Content-Type` response header. */
function mediaKindFromContentType(contentType: string): MediaKind {
  const type = contentType.toLowerCase();
  if (type.startsWith("video/")) return "video";
  if (type.startsWith("audio/")) return "audio";
  return "link";
}

// Cache probe results per URL so the same link isn't re-fetched on every render.
const mediaKindCache = new Map<string, MediaKind>();

/**
 * Render audio/video URLs inline instead of opening them as a link, so a
 * click on a media URL plays it directly rather than navigating away.
 *
 * Detection order: file extension first (cheap), then a HEAD request to read
 * the `Content-Type` header for extension-less URLs (e.g. `/play?id=123`).
 */
function MessageLink(
  props: ComponentProps<"a"> & { node?: unknown },
): ReactElement | null {
  const { href, children, className, node: _node, ...rest } = props;
  const url = typeof href === "string" ? href : "";

  const extKind = url ? mediaKindFromExtension(url) : null;
  const [probedKind, setProbedKind] = useState<MediaKind | null>(() =>
    url ? mediaKindCache.get(url) ?? null : null,
  );

  useEffect(() => {
    // Skip probing when the extension already classified the URL, when we have
    // a cached result, or for non-http(s) URLs (incl. streaming placeholders).
    if (!url || extKind || mediaKindCache.has(url) || !/^https?:\/\//i.test(url)) {
      return;
    }

    let cancelled = false;
    const controller = new AbortController();

    void (async () => {
      try {
        const res = await fetch(url, { method: "HEAD", signal: controller.signal });
        const kind = mediaKindFromContentType(res.headers.get("content-type") ?? "");
        mediaKindCache.set(url, kind);
        if (!cancelled && kind !== "link") setProbedKind(kind);
      } catch {
        // Network/CORS failure — treat as a normal link and don't retry.
        mediaKindCache.set(url, "link");
      }
    })();

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [url, extKind]);

  const kind = extKind ?? probedKind;

  if (url && kind === "video") {
    return (
      <video className={cn("my-2 max-w-full rounded-md", className)} controls src={url}>
        {children}
      </video>
    );
  }

  if (url && kind === "audio") {
    return (
      <audio className={cn("my-2 w-full", className)} controls src={url}>
        {children}
      </audio>
    );
  }

  return (
    <a
      className={className}
      href={href}
      rel="noreferrer"
      target="_blank"
      {...rest}
    >
      {children}
    </a>
  );
}

const DEFAULT_MESSAGE_COMPONENTS = { a: MessageLink } as const;

export const MessageResponse = memo(({ className, components, ...props }: MessageResponseProps) => (
  <Streamdown
    className={cn("size-full [&>*:first-child]:mt-0 [&>*:last-child]:mb-0", className)}
    components={{ ...DEFAULT_MESSAGE_COMPONENTS, ...components }}
    // Disable Streamdown's built-in linkSafety confirm modal so URL clicks
    // navigate directly (or play inline via the `a` override above for media).
    linkSafety={{ enabled: false }}
    plugins={streamdownPlugins}
    {...props}
  />
));

MessageResponse.displayName = "MessageResponse";

export type MessageToolbarProps = ComponentProps<"div">;

export const MessageToolbar = ({ className, children, ...props }: MessageToolbarProps) => (
  <div className={cn("mt-4 flex w-full items-center justify-between gap-4", className)} {...props}>
    {children}
  </div>
);
