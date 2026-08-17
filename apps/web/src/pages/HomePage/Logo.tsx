import type { ComponentPropsWithoutRef } from "react";

import { BrandLockup } from "@/components/branding/BrandLogo";

export const Logo = (
  props: ComponentPropsWithoutRef<"div">,
) => {
  return (
    <BrandLockup
      subtitle="Agent Workspace"
      titleClassName="text-[1rem]"
      subtitleClassName="text-nano"
      markClassName="h-8 w-8"
      {...props}
    />
  );
};
